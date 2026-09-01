from datetime import datetime, timezone
from types import SimpleNamespace

from golddesk.service import DeskService


class Busy:
    def done(self):
        return False


def service_with_pending(tf):
    service = DeskService.__new__(DeskService)
    service.cfg = SimpleNamespace(entry_tf="M5")
    service._analysis_future = Busy()
    service._analysis_pending = (("old",), {}, tf,
                                 datetime(2026, 9, 1, tzinfo=timezone.utc))
    return service


def test_secondary_m1_cannot_evict_guaranteed_m5_packet():
    service = service_with_pending("M5")
    before = service._analysis_pending
    service._submit_analysis(("new",), {}, "M1",
                             datetime(2026, 9, 1, 0, 1, tzinfo=timezone.utc))
    assert service._analysis_pending is before


def test_fresh_m5_replaces_queued_secondary_m1_packet():
    service = service_with_pending("M1")
    service._submit_analysis(("new",), {}, "M5",
                             datetime(2026, 9, 1, 0, 5, tzinfo=timezone.utc))
    assert service._analysis_pending[0] == ("new",)
    assert service._analysis_pending[2] == "M5"
