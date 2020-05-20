# First Party
from smdebug.profiler.MetricsReader import LocalMetricsReader, S3MetricsReader


def test_S3MetricsReader():
    bucket_name = "tornasole-dev"
    tt = S3MetricsReader(bucket_name)
    events = tt.get_events(1589930980, 1589930995)
    print(f"Number of events {len(events)}")


def test_LocalMetricsReader(tracefolder="./tests/profiler/test_traces"):
    lt = LocalMetricsReader(tracefolder)
    events = lt.get_events(1589930980, 1589930995)
    print(f"Number of events {len(events)}")
