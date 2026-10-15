"""Kafka 없이 JSON fixture를 공통 처리기와 실제 Neptune 어댑터로 전달한다."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from app.adapters.neptune_graph import NeptuneProjectionStore
from app.projection.fake import FakeConsumer, FakeRecord
from app.projection.models import ApplyResult, Identifier, ProjectionChange
from app.projection.service import GraphProjectionHandler


class FixtureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    partition: StrictInt = Field(default=0, ge=0)
    offset: StrictInt = Field(ge=0)
    key: Identifier
    change: ProjectionChange


def load_records(path: Path) -> tuple[FakeRecord, ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("Fixture must be a nonempty array of records")
    records = [FixtureRecord.model_validate(record) for record in data]
    return tuple(
        FakeRecord(
            key=record.key,
            change=record.change,
            partition=record.partition,
            offset=record.offset,
        )
        for record in records
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--endpoint", help="Writer base URL; default: NEPTUNE_ENDPOINT")
    parser.add_argument("--dry-run", action="store_true", help="Validate without IO")
    args = parser.parse_args(argv)
    try:
        records = load_records(args.fixture)
        # Fixture의 순서와 key도 통신 전에 검증한다.
        handler = GraphProjectionHandler(_ValidationStore())
        validation_consumer = FakeConsumer(handler, records)
        validation_consumer.run()
        if validation_consumer.failures:
            raise ValueError("Fixture project keys must match their changes")
        if args.dry_run:
            print(json.dumps({"validated_records": len(records), "writes": 0}))
            return 0
        with NeptuneProjectionStore.from_env(endpoint=args.endpoint) as store:
            consumer = FakeConsumer(GraphProjectionHandler(store), records)
            consumer.run()
        print(
            json.dumps(
                {
                    "results": [
                        {
                            "event_id": record.change.event_id,
                            "partition": record.partition,
                            "offset": record.offset,
                            "result": result,
                        }
                        for record, result in consumer.results
                    ],
                    "committed_offsets": consumer.committed_offsets,
                    "paused_partitions": sorted(consumer.failures),
                }
            )
        )
        if consumer.failures:
            for partition, failure in consumer.failures.items():
                print(f"partition {partition}: {failure}", file=sys.stderr)
            return 1
        return 0
    except ValidationError as exc:
        locations = [".".join(map(str, error["loc"])) for error in exc.errors()]
        print(f"Invalid fixture fields: {', '.join(locations)}", file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(f"Graph fixture failed: {exc}", file=sys.stderr)
    return 1


class _ValidationStore:
    def apply(self, change: ProjectionChange) -> ApplyResult:
        return ApplyResult.APPLIED


if __name__ == "__main__":
    raise SystemExit(main())
