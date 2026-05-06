from pathlib import Path

from archiver.response import FeedResponse
from datetime import datetime
from archiver.logger import logger
import json


class LocalWriter:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(base_dir)

    def write(self, feed_name: str, response: FeedResponse) -> None:
        date: datetime = response.get_datetime()
        file_path = (
            self.base_dir
            / feed_name
            / "raw"
            / f"year={date.year}"
            / f"month={date.month}"
            / f"day={date.day}"
            / f"{response.get_timestamp()}.bin"
        )
        file_path.parent.mkdir(parents=True, exist_ok=True)

        metadata_file_path = (
            self.base_dir
            / feed_name
            / "metadata"
            / f"year={date.year}"
            / f"month={date.month}"
            / f"day={date.day}"
            / "data.jsonl"
        )
        metadata_file_path.parent.mkdir(parents=True, exist_ok=True)

        payload = response.raw_payload()

        if payload is not None:
            tmp_path = file_path.with_suffix(".bin.tmp")
            with open(tmp_path, "wb") as f:
                f.write(payload)
            tmp_path.rename(file_path)

        else:
            logger.info("No content to persist")

        self._write_metadata(response, metadata_file_path)

    def _write_metadata(
        self,
        response: FeedResponse,
        file_path: Path,
    ):
        record = response.to_metadata_row()
        with file_path.open("a") as f:
            f.write(json.dumps(record) + "\n")
