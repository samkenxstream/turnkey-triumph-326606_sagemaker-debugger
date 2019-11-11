# Standard Library
import os
import re
import time
from abc import ABC, abstractmethod
from bisect import bisect_left

# First Party
from tornasole.analysis.utils import refresh
from tornasole.core import index_reader
from tornasole.core.access_layer.utils import has_training_ended
from tornasole.core.config_constants import (
    TRAINING_END_DELAY_REFRESH_DEFAULT,
    TRAINING_END_DELAY_REFRESH_KEY,
)
from tornasole.core.locations import IndexFileLocationUtils, TensorFileLocation, TensorLocation
from tornasole.core.logger import get_logger
from tornasole.core.modes import ModeKeys
from tornasole.core.reductions import TORNASOLE_REDUCTIONS_PREFIX, reverse_reduction_tensor_name
from tornasole.core.tensor import StepState, Tensor
from tornasole.core.utils import flatten, get_worker_name_from_collection_file, serialize_tf_device
from tornasole.exceptions import *


class EventFileTensor:
    def __init__(
        self, filename, tensor_name, step_num, tensor_value, mode=None, mode_step=None, worker=None
    ):
        self.location = TensorFileLocation.load_filename(filename)
        self.tensorname = tensor_name
        self.tensor_value = tensor_value
        self.step_num = step_num
        if mode is None:
            mode = ModeKeys.GLOBAL
        if mode_step is None:
            mode_step = step_num
        self.mode = mode
        self.mode_step = mode_step
        self.worker = worker


class Trial(ABC):
    """
    Attributes:
        _tensors
        _index_tensors_dict

    ['name', '_tensors', '_mode_to_global', '_global_to_mode', 'logger', 'parallel',
    'check', 'range_steps', 'collection_manager', 'loaded_all_steps', 'cache', 'path',
    'index_tensors_dict', 'index_mode', 'last_event_token', 'last_index_token', 'index_reader',
    'dynamic_refresh', 'trial_dir']
    """

    def __init__(
        self, name, range_steps=None, parallel=True, check=False, index_mode=True, cache=False
    ):
        self.name = name
        self._tensors = {}

        # nested dictionary from mode -> mode_step -> global_step
        # will not have global mode as a key
        self._mode_to_global = {}

        # dictionary from global_step -> (mode, mode_step)
        # can have global mode as a value
        self._global_to_mode = {}

        self.logger = get_logger()
        self.parallel = parallel
        self.check = check
        self.range_steps = range_steps
        self.collection_manager = None
        self.loaded_all_steps = False
        self.cache = cache
        self.path = None
        self.index_tensors_dict = {}
        self.index_mode = index_mode
        self.last_event_token = None
        self.last_index_token = None
        self.index_reader = index_reader.IndexReader
        self.worker_set = set()
        self.num_workers = 0
        self.workers_for_step = {}
        self.last_complete_step = -1

        """
        TORNASOLE_INCOMPLETE_STEP_WAIT_WINDOW defines the maximum number
        of incomplete steps that the trial will wait for before marking
        half of them as complete.
        """
        self.incomplete_wait_for_step_window = int(
            os.getenv("TORNASOLE_INCOMPLETE_STEP_WAIT_WINDOW", 1000)
        )

        # this is turned off during rule invocation for performance reasons since
        # required tensors are already fetched
        self.dynamic_refresh = True
        # number of seconds to wait before refreshing after seeing end of trial
        self.training_end_delay_refresh = int(
            os.getenv(TRAINING_END_DELAY_REFRESH_KEY, TRAINING_END_DELAY_REFRESH_DEFAULT)
        )
        if self.range_steps is not None:
            assert self.range_steps[0] is None or (
                isinstance(self.range_steps[0], int) and self.range_steps[0] >= 0
            )
            assert self.range_steps[1] is None or (
                isinstance(self.range_steps[1], int) and self.range_steps[1] >= 0
            )
            if self.range_steps[1] is not None and self.range_steps[0] is not None:
                assert int(self.range_steps[1]) > int(self.range_steps[0]), (
                    "range_steps should be of the form " "(begin, end) where begin is less than end"
                )
            if self.range_steps[0] is not None and self.range_steps[1] is not None:
                self.logger.info(
                    "Trial {} will look for steps between {} and {}".format(
                        self.name, self.range_steps[0], self.range_steps[1]
                    )
                )

    def __repr__(self):
        return (
            f"<{self.__class__.__module__}.{self.__class__.__name__} object at {hex(id(self))}>:(\n"
            f"    name={self.name},\n"
            f"    path={self.path},\n"
            f"    steps={self.steps()},\n"
            f"    collections={list(self.collections().keys())},\n"
            f"    tensors={self.tensors()},\n"
            f")"
        )

    @abstractmethod
    def read_collections(self, collection_files):
        pass

    @abstractmethod
    def get_collection_files(self):
        pass

    def _load_collections(self):
        num_times_before_warning = 10
        collection_files = []

        def _fetch():
            nonlocal collection_files
            nonlocal num_times_before_warning
            collection_files = self.get_collection_files()

            num_times_before_warning -= 1
            if num_times_before_warning < 0:
                self.logger.warning("Waiting to read collections")
            else:
                self.logger.debug("Waiting to read collections")

        def _wait_for_first_collection_file():
            while len(collection_files) == 0:
                time.sleep(2)
                _fetch()

        def _wait_for_all_collection_files():
            while len(collection_files) < self.num_workers:
                time.sleep(2)
                _fetch()
            for collection_file in collection_files:
                self.worker_set.add(get_worker_name_from_collection_file(collection_file))

        _fetch()
        _wait_for_first_collection_file()
        self.read_collections(collection_files)
        _wait_for_all_collection_files()

    @abstractmethod
    def _load_tensors_from_index_tensors(self, index_tensors_dict):
        pass

    @abstractmethod
    def _load_tensors_from_event_files(self, start_after_key=None):
        pass

    def __hash__(self):
        return hash((self.name, self.path))

    def __eq__(self, other):
        return (self.name, self.path) == (other.name, other.path)

    def maybe_refresh(self, name=None):
        if self.loaded_all_steps or not self.dynamic_refresh:
            return
        retry_count = 1
        training_ended = has_training_ended(self.path)
        if training_ended and self.loaded_all_steps is False:
            retry_count = 2
        while retry_count > 0:
            if name is None:
                self.refresh_tensors()
            else:
                self.refresh_tensor(name)
            if retry_count > 1:
                self.logger.info("Training has ended, will try to do a final refresh in 5 sec")
                time.sleep(self.training_end_delay_refresh)
            retry_count -= 1
        if training_ended is True and self.loaded_all_steps is False:
            self.loaded_all_steps = True
            self.last_complete_step = (
                sorted(self._global_to_mode.keys())[-1]
                if len(self._global_to_mode)
                else self.last_complete_step
            )  # Update last_complete_step to the last step written
            self.logger.info("Marked loaded all steps to True")
            self.logger.debug(
                f"Training Has Ended : last_complete_step was: {self.last_complete_step}"
            )
            self.logger.debug(f"Training Has Ended : last_index_token was: {self.last_index_token}")

    def refresh_tensor(self, tname, steps=None):
        # for now we load all tensors at once
        self.refresh_tensors()

    def tensor(self, tname):
        # will not show tensor if it was not written yet
        # has tensor will refresh
        if self.has_tensor(tname):
            return self._tensors[tname]
        else:
            raise TensorUnavailable(tname)

    def has_tensor(self, tname):
        # will return false if tensor was not written yet
        if tname not in self._tensors:
            self.maybe_refresh(tname)
        return tname in self._tensors

    def _populate_step_dict(self, tensor_object, step_num):
        if tensor_object.mode != ModeKeys.GLOBAL:
            if tensor_object.mode not in self._mode_to_global:
                self._mode_to_global[tensor_object.mode] = {}
            if tensor_object.mode_step not in self._mode_to_global[tensor_object.mode]:
                self._mode_to_global[tensor_object.mode][tensor_object.mode_step] = int(step_num)
        if step_num not in self._global_to_mode:
            self._global_to_mode[step_num] = (tensor_object.mode, tensor_object.mode_step)

    def _populate_workers_for_step(self, step, worker) -> None:
        """
        The self.workers_for_step dictionary holds a mapping of
        step number and a set of all the workers that have written the step.

        This function is used to add a worker to that set. To mark that a particular worker
        has finished writing the step.
        :param step:
        :param worker:
        :return: None
        """
        if step not in self.workers_for_step:
            self.workers_for_step[step] = set()
        self.workers_for_step[step].add(worker)
        if len(self.workers_for_step[step]) == self.num_workers and step > self.last_complete_step:
            self.last_complete_step = step

    def add_tensor(self, step_num, worker, tensor_object):
        to = tensor_object
        # self.worker_set.add(worker)
        if TORNASOLE_REDUCTIONS_PREFIX in to.tensorname:
            tname, red_name, abs = reverse_reduction_tensor_name(to.tensorname)
        else:
            tname = to.tensorname
        if tname not in self._tensors:
            t = Tensor(tname, trial=self, cache=self.cache)
            self._tensors[tname] = t
        t = self._tensors[tname]
        self._populate_step_dict(to, step_num)
        self._populate_workers_for_step(step_num, worker)
        if TORNASOLE_REDUCTIONS_PREFIX in to.tensorname:
            if type(to) is TensorLocation:
                t.add_reduction_step_lazy(to.mode, to.mode_step, worker, red_name, abs, to)
            else:
                t.add_reduction_step(to.mode, to.mode_step, worker, red_name, abs, to.tensor_value)
        else:
            if type(to) is TensorLocation:
                t.add_step_lazy(to.mode, to.mode_step, worker, to)
            else:
                t.add_step(to.mode, to.mode_step, worker, to.tensor_value)

    def tensors(self):
        self.maybe_refresh()
        ts = list(self._tensors.keys())
        return ts

    def workers(self):
        self.maybe_refresh()
        return sorted(list(self.worker_set))

    def steps(self, mode=ModeKeys.GLOBAL, show_incomplete_steps=False) -> list:
        """
        the steps function call returns only completed steps to
        the user.
        :param mode: ModeKeys
        :param show_incomplete_steps: bool
        :return: list
        """
        all_steps = self._all_steps(mode)
        if show_incomplete_steps is True:
            return all_steps
        completed_steps = list()
        for step in all_steps:
            global_step = self._mode_to_global[mode][step] if mode != ModeKeys.GLOBAL else step
            if (
                len(self.workers_for_step[global_step]) == self.num_workers
                or self.loaded_all_steps is True
                or self.last_complete_step >= global_step
            ):
                completed_steps.append(step)
        return completed_steps

    def _all_steps(self, mode=ModeKeys.GLOBAL) -> list:
        """
        the all_steps function call returns all the steps,
        complete or incomplete the user.
        :param mode: ModeKeys
        :return: list
        """
        self.maybe_refresh()
        if mode == ModeKeys.GLOBAL:
            return sorted(self._global_to_mode.keys())
        elif mode in self._mode_to_global:
            return sorted(self._mode_to_global[mode].keys())
        else:
            return []

    def _global_step_currently(self, mode, mode_step):
        if mode == ModeKeys.GLOBAL:
            return mode_step
        elif mode in self._mode_to_global and mode_step in self._mode_to_global[mode]:
            return self._mode_to_global[mode][mode_step]

    def global_step(self, mode, mode_step):
        s = self._global_step_currently(mode, mode_step)
        if s is not None:
            return s
        else:
            self.maybe_refresh()
            return self._global_step_currently(mode, mode_step)

    def _mode_modestep_currently(self, global_step):
        if global_step in self._global_to_mode:
            return self._global_to_mode[global_step]

    def mode_modestep(self, global_step):
        x = self._mode_modestep_currently(global_step)
        if x:
            return x
        else:
            self.maybe_refresh()
            x = self._mode_modestep_currently(global_step)
            if x:
                return x
        return None, None

    def mode_step(self, global_step):
        # can return global step itself in some cases
        x = self.mode_modestep(global_step)
        if x:
            return x[1]

    def mode(self, global_step):
        # can return global mode in some cases
        x = self.mode_modestep(global_step)
        if x:
            return x[0]

    def modes(self):
        # will not return global mode
        return self._mode_to_global.keys()

    def tensors_matching_regex(self, regex_list):
        self.maybe_refresh()
        matched_tensornames = []
        if not isinstance(regex_list, list):
            regex_list = [regex_list]
        regex_list = flatten(regex_list)
        for tensorname in self._tensors.keys():
            for regex_pattern in regex_list:
                if re.match(regex_pattern, tensorname):
                    matched_tensornames.append(tensorname)
                    break
        return matched_tensornames

    def collections(self):
        return self.collection_manager.collections

    def collection(self, coll_name):
        return self.collection_manager.get(coll_name)

    def tensors_in_collection(self, coll_name):
        rval = set()
        for x in self.collection(coll_name).tensor_names:
            rval.add(x)
        regex = self.collection(coll_name).include_regex
        if regex:
            for x in self.tensors_matching_regex(regex):
                rval.add(x)
        return list(rval)

    def wait_for_steps(self, required_steps, mode=ModeKeys.GLOBAL):
        with refresh(self):
            for step in required_steps:
                while True:
                    s = self.has_passed_step(step, mode)
                    if s == StepState.UNAVAILABLE:
                        raise StepUnavailable(step, mode)
                    elif s == StepState.AVAILABLE:
                        break
                    elif self.loaded_all_steps is True:
                        last_step = -1
                        avail_steps = self._all_steps(mode=mode)
                        if len(avail_steps) > 0:
                            last_step = avail_steps[-1]
                        raise NoMoreData(step, mode, last_step)
                    time.sleep(5)

    def has_passed_step(self, step, mode=ModeKeys.GLOBAL) -> StepState:
        """
        This function indicates whether a step is complete (AVAILABLE),
        incomplete ( NOT_YET_AVAILABLE ) or absent ( UNAVAILABLE ).

        Overview of logic:

            1. if the queried step is greater than all the available steps (complete / incomplete):
                if job is not complete:
                    return StepState.NOT_YET_AVAILABLE
                else:
                    return StepState.UNAVAILABLE
            2. if the queried step is less or equal to a step in available steps (complete / incomplete):
                if the queried step is less than all the available steps:
                    if single_worker:
                        return UNAVAILABLE ( step has been skipped or will not written)
                    else:
                        return NOT_YET_AVAILABLE
            3. queried step is available:
                if all workers have written the step or job is complete
                or last_complete_step > step ( All workers have written a step greater than the step we are checking.
                                                    Hence, the step will never be complete. )
                    return AVAILABLE
                else:
                     return NOT_YET_AVAILABLE
        :param step: str
        :param mode: ModeKeys.GLOBAL
        :return: StepState
        """
        all_steps = self.steps(mode=mode, show_incomplete_steps=True)
        bisect_idx = bisect_left(all_steps, step)
        if bisect_idx < len(all_steps):
            if all_steps[bisect_idx] > step:
                if self.last_complete_step > step:
                    return StepState.UNAVAILABLE
                return StepState.NOT_YET_AVAILABLE
            elif all_steps[bisect_idx] == step:
                if len(self.workers_for_step[step]) == self.num_workers:
                    return StepState.AVAILABLE
                elif self.loaded_all_steps is True:
                    self.logger.info(
                        f"Step: {step} was written only by workers: {self.workers_for_step[step]}"
                    )
                    self.logger.info(
                        f"Step: {step} was marked complete because the job is complete"
                    )
                    return StepState.AVAILABLE
                elif step <= self.last_complete_step:
                    self.logger.info(
                        f"Step: {step} was written only by workers: {self.workers_for_step[step]}"
                    )
                    self.logger.info(
                        f"Step: {step} was marked complete because the last complete step is {self.last_complete_step}"
                    )
                    return StepState.AVAILABLE
                else:
                    return StepState.NOT_YET_AVAILABLE
        if self.loaded_all_steps is True:
            return StepState.UNAVAILABLE
        return StepState.NOT_YET_AVAILABLE

    def _add_tensors_at_steps(self, event_file_tensors):
        for eft in event_file_tensors:
            self.add_tensor(eft.step_num, worker=eft.worker, tensor_object=eft)

    def load_tensors(self):
        if self.index_mode:
            self._load_tensors_from_index_files()
        else:
            self._load_tensors_from_event_files()

    def _update_last_index_token(self, new_index_token: str) -> None:
        """
        This function updates the last_index_token in the following scenarios:
            1. last_complete_step > last_index_token_step :
                this means that the token isn't pointing to the latest completed step
            2. number of steps available ( complete or incomplete ) - (last_completed_step+1) > window_size_limit:
                we maintain a window to stop querying for older steps that have not completed.
                if the total number of steps, we are querying for completion is greater than our window_size_limit
                we update the last_index_token and last_complete_step by (window_size_limit // 2)
        :param new_index_token:
        :return:None
        """
        if self.last_index_token is None:
            last_index_token_step = 0
        else:
            last_index_token_step = IndexFileLocationUtils.parse_step_from_index_file_name(
                self.last_index_token
            )

        # Case 1:
        if self.last_complete_step > last_index_token_step:
            prefix = IndexFileLocationUtils.get_prefix_from_index_file(new_index_token)
            # sort lexicographically and select the last worker
            last_worker = sorted(list(self.worker_set))[-1]
            # below converts worker_name to serialized workerName
            # if it's a tf device, else no effect
            last_worker_serialized = serialize_tf_device(last_worker)
            self.last_index_token = IndexFileLocationUtils.get_index_key_for_step(
                prefix, self.last_complete_step, last_worker_serialized
            )

        # Case 2:
        available_step = self._global_to_mode.keys()
        if (
            len(available_step) - (self.last_complete_step + 1)
            > self.incomplete_wait_for_step_window
        ):
            prefix = IndexFileLocationUtils.get_prefix_from_index_file(new_index_token)
            last_worker = sorted(list(self.worker_set))[-1]
            # below converts worker_name to serialized workerName
            # if it's a tf device, else no effect
            last_worker_serialized = serialize_tf_device(last_worker)
            self.last_index_token = IndexFileLocationUtils.get_index_key_for_step(
                prefix,
                self.last_complete_step + (self.incomplete_wait_for_step_window // 2),
                last_worker_serialized,
            )
            self.last_complete_step = IndexFileLocationUtils.parse_step_from_index_file_name(
                self.last_index_token
            )
            self.logger.info(
                f"Waiting for: {len(available_step) - (self.last_complete_step + 1)} Steps. \n"
                f"TORNASOLE_INCOMPLETE_STEP_WAIT_WINDOW: {self.incomplete_wait_for_step_window}. \n"
                f"Marking the last {self.incomplete_wait_for_step_window // 2} incomplete steps as complete"
                f"Updating last_index_token to: {self.last_index_token}. \n"
                f"Updating last_complete_step to: {self.last_complete_step}. "
            )

    def _load_tensors_from_index_files(self):
        self.index_tensors_dict, new_index_token = self.index_reader.load_tensor_data_from_index_files(
            self.path, start_after_key=self.last_index_token, range_steps=self.range_steps
        )
        self._load_tensors_from_index_tensors(self.index_tensors_dict)
        if new_index_token:  # new index token can be None if there are no new index files
            self._update_last_index_token(new_index_token)

    def refresh_tensors(self):
        # TODO if job finished
        if self.index_mode:
            index_tensors_dict, new_index_token = self.index_reader.load_tensor_data_from_index_files(
                self.path, start_after_key=self.last_index_token, range_steps=self.range_steps
            )
            if len(index_tensors_dict):
                self.index_tensors_dict.update(index_tensors_dict)
                self._load_tensors_from_index_tensors(index_tensors_dict)
            if new_index_token:  # new index token can be None if there are no new index files
                self._update_last_index_token(new_index_token)
        else:
            self._load_tensors_from_event_files(start_after_key=self.last_event_token)
