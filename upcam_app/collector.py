"""Data source boundary; the current checked-out repository has no API collector."""


class CollectorUnavailable(RuntimeError):
    pass


class TeambitionCollector:
    def fetch(self, project_id, start, end, credentials):
        raise CollectorUnavailable(
            "This repository revision contains no Teambition read-only collector or identity mapping rules. "
            "No network request was made."
        )
