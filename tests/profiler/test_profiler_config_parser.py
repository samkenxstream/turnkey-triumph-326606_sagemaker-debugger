# Standard Library
import json
import os

# Third Party
import pytest
from tests.profiler.profiler_config_parser_utils import current_step, detailed_profiling_test_cases

# First Party
from smdebug.profiler.profiler_config_parser import ProfilerConfigParser
from smdebug.profiler.profiler_constants import (
    CLOSE_FILE_INTERVAL_DEFAULT,
    DEFAULT_PREFIX,
    FILE_OPEN_FAIL_THRESHOLD_DEFAULT,
    MAX_FILE_SIZE_DEFAULT,
)


@pytest.fixture
def detailed_profiler_config_path(config_folder, monkeypatch):
    config_path = os.path.join(config_folder, "detailed_profiler_config.json")
    monkeypatch.setenv("SMPROFILER_CONFIG_PATH", config_path)
    yield config_path
    if os.path.isfile(config_path):
        os.remove(config_path)


@pytest.fixture
def missing_config_profiler_config_parser(config_folder, monkeypatch):
    config_path = os.path.join(config_folder, "missing_profile_config_parser.json")  # doesn't exist
    monkeypatch.setenv("SMPROFILER_CONFIG_PATH", config_path)
    return ProfilerConfigParser(current_step)


@pytest.fixture
def user_disabled_profiler_config_parser(config_folder, monkeypatch):
    config_path = os.path.join(config_folder, "user_disabled_profile_config_parser.json")
    monkeypatch.setenv("SMPROFILER_CONFIG_PATH", config_path)
    return ProfilerConfigParser(current_step)


@pytest.mark.parametrize("test_case", detailed_profiling_test_cases)
def test_profiling_ranges(detailed_profiler_config_path, test_case):
    detailed_profiling_parameters, expected_values = test_case
    start_step, num_steps, start_time, duration = detailed_profiling_parameters
    detailed_profiler_config = {}
    if start_step:
        detailed_profiler_config.update(StartStep=start_step)
    if num_steps:
        detailed_profiler_config.update(NumSteps=num_steps)
    if start_time:
        detailed_profiler_config.update(StartTime=start_time)
    if duration:
        detailed_profiler_config.update(Duration=duration)

    full_config = {
        "ProfilingParameters": {
            "ProfilerEnabled": True,
            "DetailedProfilingConfig": detailed_profiler_config,
        }
    }

    with open(detailed_profiler_config_path, "w") as f:
        json.dump(full_config, f)

    profiler_config_parser = ProfilerConfigParser(current_step)
    profile_range = profiler_config_parser.config.profile_range
    expected_profiler_start, expected_profiler_end, expected_can_profile = expected_values

    assert profile_range.profiler_start == expected_profiler_start
    assert profile_range.profiler_end == expected_profiler_end
    assert profile_range.can_enable_profiling(current_step) == expected_can_profile


def test_disabled_profiler(
    missing_config_profiler_config_parser, user_disabled_profiler_config_parser
):
    """
    This test is meant to test that a missing config file or the user setting `ProfilerEnabled`
    to `false` will disable the profiler.
    """
    assert not missing_config_profiler_config_parser.enabled
    assert not user_disabled_profiler_config_parser.enabled


def test_default_values(simple_profiler_config_parser):
    """
    This test is meant to test setting default values when the config is present.
    """
    assert simple_profiler_config_parser.enabled

    trace_file_config = simple_profiler_config_parser.config.trace_file
    assert trace_file_config.file_open_fail_threshold == FILE_OPEN_FAIL_THRESHOLD_DEFAULT

    rotation_policy = trace_file_config.rotation_policy
    assert rotation_policy.file_max_size == MAX_FILE_SIZE_DEFAULT
    assert rotation_policy.file_close_interval == CLOSE_FILE_INTERVAL_DEFAULT

    profile_range = simple_profiler_config_parser.config.profile_range
    assert not profile_range.profile_type
    assert not profile_range.profiler_start
    assert not profile_range.profiler_end
