#!/usr/bin/env python3
"""Create an immutable filtered handoff bundle for selected search queries."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import merge_semantic_reviews as filters
import stamp_agent_codebook_annotations as annotation_tools


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_SOURCE = DATA_DIR / "raw_data" / "dedup_posts.csv"
PACIFIC_TIME = ZoneInfo("America/Los_Angeles")
RESULT_CLASSIFICATIONS = {"INCLUDE", "UNCERTAIN"}


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        return fieldnames, list(reader)


def write_csv(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> None:
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def split_queries(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(";") if item.strip()]


def restore_aggression_only(
    decision: dict[str, str],
    language_decision: dict[str, str] | None,
) -> dict[str, str]:
    """Replace an aggression-only exclusion with the next filter result."""
    if decision["curation_exclusion_reason"] != "AGGRESSION_QUERY_ONLY":
        return decision
    if language_decision is None:
        return {
            "curation_status": "RETAIN",
            "curation_exclusion_reason": "",
            "curation_language": "English",
            "curation_rationale": "Submission is primarily in English.",
        }
    if language_decision["language_classification"] == "NON_ENGLISH":
        return {
            "curation_status": "EXCLUDE",
            "curation_exclusion_reason": "NON_ENGLISH",
            "curation_language": language_decision["detected_language"],
            "curation_rationale": language_decision[
                "classification_rationale"
            ],
        }
    return {
        "curation_status": "REVIEW",
        "curation_exclusion_reason": "LANGUAGE_UNCERTAIN",
        "curation_language": language_decision["detected_language"],
        "curation_rationale": language_decision[
            "classification_rationale"
        ],
    }


def read_annotation_ids(path: Path) -> set[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "post_id" not in reader.fieldnames:
            raise ValueError(f"{path} has no post_id column")
        post_ids = [row["post_id"].strip().casefold() for row in reader]
    if any(not post_id for post_id in post_ids):
        raise ValueError(f"{path} contains a blank post ID")
    if len(post_ids) != len(set(post_ids)):
        raise ValueError(f"{path} contains duplicate post IDs")
    return set(post_ids)


def timestamped_output_dir(moment: datetime | None = None) -> Path:
    generated_at = moment or datetime.now(timezone.utc)
    stamp = generated_at.astimezone(PACIFIC_TIME).strftime(
        "%Y-%m-%d_%H-%M-%S"
    )
    return DATA_DIR / stamp


def parse_pacific_datetime(value: str) -> datetime:
    """Parse an ISO date/time and interpret a missing offset as Pacific."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=PACIFIC_TIME)
    return parsed.astimezone(PACIFIC_TIME)


def row_pacific_datetime(value: str) -> datetime:
    """Parse the stored Pacific wall time and restore the regional zone."""
    try:
        wall_time = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"Invalid datetime_pst value: {value!r}") from exc
    return wall_time.replace(tzinfo=PACIFIC_TIME)


def rebuild_classified_source(
    raw_fields: list[str],
    raw_rows: list[dict[str, str]],
) -> tuple[list[str], list[dict[str, str]]]:
    """Rebuild classified rows from the preserved semantic review shards."""
    decisions: dict[int, dict[str, str]] = {}
    for shard in filters.SHARDS:
        if not shard.exists():
            raise FileNotFoundError(f"Missing semantic review shard: {shard}")
        for record in filters.read_shard(shard):
            index = int(record["row_index"])
            if shard == filters.INITIAL_SHARD and index in filters.CORRECTED_INDICES:
                continue
            if index < 0 or index >= len(raw_rows):
                raise ValueError(f"{shard}: row_index {index} is outside source bounds")
            if index in decisions:
                raise ValueError(f"Duplicate semantic decision for row_index {index}")
            label = record["classification"].strip().upper()
            if label not in filters.VALID_LABELS:
                raise ValueError(f"{shard}: invalid classification {label!r}")
            provided_link = (record.get("post_link") or "").strip()
            if provided_link and provided_link != raw_rows[index]["post_link"]:
                raise ValueError(f"{shard}: row {index} link mismatch")
            rationale = (record.get("classification_rationale") or "").strip()
            if not rationale:
                raise ValueError(f"{shard}: row {index} has no rationale")
            decisions[index] = {
                "classification": label,
                "exclusion_reason": filters.canonical_reason(
                    label,
                    (record.get("exclusion_reason") or "").strip(),
                ),
                "classification_rationale": rationale,
            }

    expected_indices = set(range(len(raw_rows)))
    if set(decisions) != expected_indices:
        raise ValueError("Semantic review shards do not cover the raw source")
    language_decisions = filters.read_language_decisions(raw_rows, decisions)
    fieldnames = raw_fields + [
        "review_row_index",
        "classification",
        "exclusion_reason",
        "classification_rationale",
        *filters.CURATION_FIELDS,
    ]
    classified = [
        {
            **row,
            "review_row_index": str(index),
            **decisions[index],
            **filters.curation_decision(
                row,
                decisions[index]["classification"],
                language_decisions.get(index),
            ),
        }
        for index, row in enumerate(raw_rows)
    ]
    return fieldnames, classified


def build_bundle(
    source_bundle: Path,
    selected_queries: list[str],
    output_dir: Path | None = None,
    *,
    retain_aggression_only: bool = False,
    required_annotations: Path | None = None,
    withhold_missing_annotations: bool = False,
    start_pacific: datetime | None = None,
    end_pacific_exclusive: datetime | None = None,
) -> tuple[Path, dict[str, int]]:
    classified_path = source_bundle / "dedup_posts_classified.csv"
    raw_fields, raw_rows = read_csv(RAW_SOURCE)
    if classified_path.exists():
        fieldnames, classified_source = read_csv(classified_path)
    else:
        fieldnames, classified_source = rebuild_classified_source(
            raw_fields,
            raw_rows,
        )
    if raw_fields != fieldnames[: len(raw_fields)]:
        raise ValueError("Raw and classified source columns do not align")
    if len(raw_rows) != len(classified_source):
        raise ValueError("Raw and classified source row counts differ")
    raw_links = [row["post_link"] for row in raw_rows]
    classified_links = [row["post_link"] for row in classified_source]
    if raw_links != classified_links:
        raise ValueError("Raw and classified source post order differs")
    if len(raw_links) != len(set(raw_links)):
        raise ValueError("Raw source post links are not unique")

    semantic_decisions = {
        int(row["review_row_index"]): {
            "classification": row["classification"]
        }
        for row in classified_source
    }
    expected_indices = set(range(len(raw_rows)))
    if set(semantic_decisions) != expected_indices:
        raise ValueError("Classified source review indices are incomplete")
    language_decisions = filters.read_language_decisions(
        raw_rows,
        semantic_decisions,
    )
    annotation_ids = (
        read_annotation_ids(required_annotations)
        if required_annotations is not None
        else None
    )
    if withhold_missing_annotations and annotation_ids is None:
        raise ValueError(
            "Withholding missing annotations requires an annotation matrix"
        )

    canonical_queries: dict[str, str] = {}
    for row in classified_source:
        for query in split_queries(row["matching_queries"]):
            canonical_queries.setdefault(query.casefold(), query)

    requested = [query.strip().casefold() for query in selected_queries]
    if not requested or any(not query for query in requested):
        raise ValueError("At least one nonblank query is required")
    if len(requested) != len(set(requested)):
        raise ValueError("Selected queries must be unique")
    unknown = sorted(set(requested) - set(canonical_queries))
    if unknown:
        raise ValueError(f"Unknown selected queries: {unknown}")
    selected = set(requested)

    if start_pacific is not None:
        start_pacific = start_pacific.astimezone(PACIFIC_TIME)
    if end_pacific_exclusive is not None:
        end_pacific_exclusive = end_pacific_exclusive.astimezone(PACIFIC_TIME)
    if (
        start_pacific is not None
        and end_pacific_exclusive is not None
        and start_pacific >= end_pacific_exclusive
    ):
        raise ValueError("Pacific start must be earlier than exclusive end")

    classified: list[dict[str, str]] = []
    query_selected_count = 0
    date_excluded_count = 0
    for source_row in classified_source:
        memberships = [
            query
            for query in split_queries(source_row["matching_queries"])
            if query.casefold() in selected
        ]
        if not memberships:
            continue
        query_selected_count += 1
        created_pacific = row_pacific_datetime(source_row["datetime_pst"])
        if (
            start_pacific is not None
            and created_pacific < start_pacific
        ) or (
            end_pacific_exclusive is not None
            and created_pacific >= end_pacific_exclusive
        ):
            date_excluded_count += 1
            continue
        row = dict(source_row)
        row["matching_queries"] = "; ".join(memberships)
        review_index = int(row["review_row_index"])
        decision = filters.curation_decision(
            row,
            row["classification"],
            language_decisions.get(review_index),
        )
        if retain_aggression_only:
            decision = restore_aggression_only(
                decision,
                language_decisions.get(review_index),
            )
        row.update(decision)
        if (
            annotation_ids is not None
            and row["classification"] in RESULT_CLASSIFICATIONS
            and row["curation_status"] != "EXCLUDE"
            and annotation_tools.post_id_from_link(row["post_link"])
            not in annotation_ids
            and not withhold_missing_annotations
        ):
            row.update(
                {
                    "curation_status": "EXCLUDE",
                    "curation_exclusion_reason": (
                        "MISSING_CODEBOOK_ANNOTATION"
                    ),
                    "curation_language": "",
                    "curation_rationale": (
                        "Post has no existing binary codebook annotation; "
                        "excluded to preserve the required paired matrix."
                    ),
                }
            )
        classified.append(row)

    false_positives = [
        row for row in classified if row["classification"] == "FALSE_POSITIVE"
    ]
    uncertain = [
        row for row in classified if row["classification"] == "UNCERTAIN"
    ]
    curation_exclusions = [
        row for row in classified if row["curation_status"] == "EXCLUDE"
    ]
    language_uncertain = [
        row
        for row in classified
        if row["curation_exclusion_reason"] == "LANGUAGE_UNCERTAIN"
    ]
    retained = [
        row
        for row in classified
        if row["classification"] in RESULT_CLASSIFICATIONS
        and row["curation_status"] != "EXCLUDE"
    ]
    annotation_withheld = (
        [
            row
            for row in retained
            if annotation_tools.post_id_from_link(row["post_link"])
            not in annotation_ids
        ]
        if annotation_ids is not None
        else []
    )

    destination = output_dir or timestamped_output_dir()
    destination.mkdir(parents=True, exist_ok=False)
    write_csv(destination / "dedup_posts.csv", fieldnames, retained)

    if required_annotations is not None:
        annotation_ready = [
            row
            for row in retained
            if annotation_tools.post_id_from_link(row["post_link"])
            in annotation_ids
        ]
        terms = annotation_tools.read_codebook()
        annotations = annotation_tools.read_annotations(
            required_annotations,
            terms,
        )
        write_csv(
            destination / "agent_codebook_annotations.csv",
            ["post_id", *terms],
            [
                annotations[
                    annotation_tools.post_id_from_link(row["post_link"])
                ]
                for row in annotation_ready
            ],
        )

    metadata = filters.current_filter_metadata(classified)
    aggression_candidates = sum(
        row["classification"] in RESULT_CLASSIFICATIONS
        and not filters.is_removed_body(row["body"])
        and not filters.is_deleted_body(row["body"])
        and not filters.is_blank_body(row["body"])
        and not filters.is_link_only_body(row["body"])
        and filters.is_withheld_query_only(row["matching_queries"])
        for row in classified
    )
    if retain_aggression_only:
        aggression_metadata = next(
            row
            for row in metadata
            if row["rule_id"] == "AGGRESSION_QUERY_ONLY"
        )
        aggression_metadata.update(
            {
                "action": "RETAIN",
                "Removal criteria": (
                    "retained category: search-term membership contains "
                    "only aggression-related selected queries"
                ),
                "records_matched": str(aggression_candidates),
                "records_after": aggression_metadata["records_before"],
                "notes": (
                    "Previously excluded aggression-only records are "
                    "restored in this bundle."
                ),
            }
        )
    missing_annotation_count = (
        len(annotation_withheld)
        if withhold_missing_annotations
        else sum(
            row["curation_exclusion_reason"]
            == "MISSING_CODEBOOK_ANNOTATION"
            for row in classified
        )
    )
    if required_annotations is not None:
        records_before = int(metadata[-1]["records_after"])
        if withhold_missing_annotations:
            metadata.append(
                {
                    "stage_order": "3",
                    "stage_name": "agent_codebook_routing",
                    "rule_order": "1",
                    "rule_id": "MISSING_CODEBOOK_ANNOTATION",
                    "action": "WITHHOLD_FROM_AGENT_CODEBOOK",
                    "Removal criteria": (
                        "has no existing binary codebook annotation"
                    ),
                    "records_before": str(records_before),
                    "records_matched": str(missing_annotation_count),
                    "records_after": str(records_before),
                    "notes": (
                        "Retained in dedup_posts.csv and omitted from "
                        "agent_codebook_annotations.csv."
                    ),
                }
            )
        else:
            metadata.append(
                {
                    "stage_order": "2",
                    "stage_name": "deterministic_curation",
                    "rule_order": str(int(metadata[-1]["rule_order"]) + 1),
                    "rule_id": "MISSING_CODEBOOK_ANNOTATION",
                    "action": "EXCLUDE",
                    "Removal criteria": (
                        "has no existing binary codebook annotation"
                    ),
                    "records_before": str(records_before),
                    "records_matched": str(missing_annotation_count),
                    "records_after": str(
                        records_before - missing_annotation_count
                    ),
                    "notes": (
                        "Excluded to keep post and annotation IDs identical "
                        "and in the same order."
                    ),
                }
            )
    prefilter_stage_count = 1 + int(
        start_pacific is not None or end_pacific_exclusive is not None
    )
    for row in metadata:
        row["stage_order"] = str(
            int(row["stage_order"]) + prefilter_stage_count
        )
    selected_names = [canonical_queries[query] for query in requested]
    selection_row = {
        "stage_order": "1",
        "stage_name": "query_selection",
        "rule_order": "1",
        "rule_id": "UNSELECTED_QUERY_ONLY",
        "action": "EXCLUDE",
        "Removal criteria": (
            "search-term membership contains none of the selected queries"
        ),
        "records_before": str(len(classified_source)),
        "records_matched": str(
            len(classified_source) - query_selected_count
        ),
        "records_after": str(query_selected_count),
        "notes": "Selected queries: " + "; ".join(selected_names),
    }
    prefilter_metadata = [selection_row]
    if start_pacific is not None or end_pacific_exclusive is not None:
        start_label = (
            start_pacific.isoformat() if start_pacific is not None else "-∞"
        )
        end_label = (
            end_pacific_exclusive.isoformat()
            if end_pacific_exclusive is not None
            else "+∞"
        )
        date_row = {
            "stage_order": "2",
            "stage_name": "date_selection",
            "rule_order": "1",
            "rule_id": "OUTSIDE_PACIFIC_TIME_RANGE",
            "action": "EXCLUDE",
            "Removal criteria": (
                f"created before {start_label} or on/after {end_label}"
            ),
            "records_before": str(query_selected_count),
            "records_matched": str(date_excluded_count),
            "records_after": str(len(classified)),
            "notes": (
                "Half-open Pacific-time interval: start included; "
                "end excluded."
            ),
        }
        prefilter_metadata.append(date_row)
    write_csv(
        destination / "filter_metadata.csv",
        filters.FILTER_METADATA_FIELDS,
        [*prefilter_metadata, *metadata],
    )

    stats = {
        "raw_source_rows": len(classified_source),
        "selected_deduplicated_rows": query_selected_count,
        "date_range_exclusions": date_excluded_count,
        "date_bounded_rows": len(classified),
        "false_positives": len(false_positives),
        "semantic_uncertain": len(uncertain),
        "curation_exclusions": len(curation_exclusions),
        "language_uncertain": len(language_uncertain),
        "aggression_only_candidates": aggression_candidates,
        "missing_annotation_posts": missing_annotation_count,
        "agent_codebook_rows": len(retained) - len(annotation_withheld),
        "retained_rows": len(retained),
        "retained_query_memberships": sum(
            len(split_queries(row["matching_queries"])) for row in retained
        ),
    }
    return destination, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", required=True, type=Path)
    parser.add_argument("--query", action="append", required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--retain-aggression-only",
        action="store_true",
        help="Retain posts whose selected memberships are aggression-only.",
    )
    parser.add_argument(
        "--require-annotations",
        type=Path,
        help="Exclude posts absent from this annotation matrix.",
    )
    parser.add_argument(
        "--withhold-missing-annotations",
        action="store_true",
        help=(
            "Keep unannotated posts in the main dataset and route only "
            "annotated posts into a nested agent_codebook pair."
        ),
    )
    parser.add_argument(
        "--start-pacific",
        type=parse_pacific_datetime,
        help="Inclusive ISO date/time bound interpreted in Pacific time.",
    )
    parser.add_argument(
        "--end-pacific-exclusive",
        type=parse_pacific_datetime,
        help="Exclusive ISO date/time bound interpreted in Pacific time.",
    )
    args = parser.parse_args()
    output_dir, stats = build_bundle(
        args.source_bundle.resolve(),
        args.query,
        args.output_dir.resolve() if args.output_dir else None,
        retain_aggression_only=args.retain_aggression_only,
        required_annotations=(
            args.require_annotations.resolve()
            if args.require_annotations
            else None
        ),
        withhold_missing_annotations=args.withhold_missing_annotations,
        start_pacific=args.start_pacific,
        end_pacific_exclusive=args.end_pacific_exclusive,
    )
    print(f"output_dir={output_dir}")
    for key, value in stats.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
