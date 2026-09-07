"""Check API record coverage and binary deployment integrity, not translation quality."""

import argparse
import json
from pathlib import Path

from nagi.gameio.deployment import yuris_assets
from nagi.gameio.yuris import (
    archive_entries,
    catalog,
    load_source,
    parse_script,
    save_json,
    sha,
)
from nagi.translation.yuris import batch_messages, batches, parse_response


def audit(workflow_root, *, save_report=False):
    workflow_root = Path(workflow_root).resolve()
    state = json.loads((workflow_root / "workflow.json").read_text(encoding="utf-8"))
    if state["stages"]["translate"]["status"] != "completed":
        raise ValueError("The real translation workflow has not completed")
    corpus = Path(state["corpus_dir"]).resolve()
    translated = Path(state["translated_scripts_dir"]).resolve()
    if workflow_root not in corpus.parents or workflow_root not in translated.parents:
        raise ValueError("Result directory does not belong to this workflow")
    source, _ = load_source(corpus, state["game_dir"])
    table, key, units = catalog(archive_entries(source))
    saved_units = json.loads((corpus / "texts.json").read_text(encoding="utf-8"))
    if units != saved_units:
        raise ValueError("Saved source text is not the complete extracted catalog")
    result = json.loads(
        (translated / "translation-result.json").read_text(encoding="utf-8")
    )
    from nagi.gameio.yuris_opening import selected_for_result

    selected = (selected_for_result(archive_entries(source), table, key, units, result)
                if result.get("mode") == "partial" else units)
    if state.get("mode") != result.get("mode", "full"):
        raise ValueError("Workflow and translation result scopes differ")
    if result["status"] != "completed" or result["translated_count"] != len(selected):
        raise ValueError("Translation result count is incomplete")
    requests_root = translated / "api-batches"
    leaf_count = 0
    from nagi.translation.route_context import prepare_route_context
    route_context = prepare_route_context(corpus, result, game_dir=state["game_dir"])
    glossary = None
    if result.get("character_glossary_version"):
        from nagi.translation.characters import GLOSSARY_FILE, load_glossary

        glossary = load_glossary(translated / GLOSSARY_FILE,
                                 expected_version=result["character_glossary_version"])

    def receipt(stem, batch):
        nonlocal leaf_count
        request_path = requests_root / f"{stem}.request.json"
        messages = (
            json.loads(request_path.read_text(encoding="utf-8"))
            if request_path.exists()
            else batch_messages(batch, glossary, route_context)
        )
        payload = json.loads(messages[1]["content"])
        if (
            payload["texts"] != batch
            or payload["source_language"] != "ja"
            or payload["target_language"] != "zh-CN"
        ):
            raise ValueError(f"API input mismatch: {stem}")
        saved = json.loads(
            (requests_root / f"{stem}.response.json").read_text(encoding="utf-8")
        )
        if saved["request_sha256"] != sha(
            json.dumps(messages, ensure_ascii=False).encode()
        ):
            raise ValueError(f"API request hash mismatch: {stem}")
        mapping = parse_response(saved["response"], batch)
        children = saved.get("assembled_from")
        if children:
            expected_children = [f"{stem}.part0", f"{stem}.part1"]
            if children != expected_children:
                raise ValueError("Unexpected child receipt paths")
            midpoint = len(batch) // 2
            combined = receipt(children[0], batch[:midpoint])
            combined.update(receipt(children[1], batch[midpoint:]))
            if combined != mapping:
                raise ValueError("Parent receipt differs from the actual child results")
        else:
            journal = requests_root / f"{stem}.received.json"
            raw = (
                json.loads(journal.read_text(encoding="utf-8"))
                if journal.exists()
                else saved
            )
            if raw["response"] != saved["response"]:
                raise ValueError("Completed receipt is not the recorded API response")
            leaf_count += 1
        return mapping

    combined = {}
    plan = list(batches(selected))
    for index, batch in enumerate(plan):
        combined.update(receipt(f"{index:05d}", batch))
    if combined != json.loads(
        (translated / "translations.json").read_text(encoding="utf-8")
    ):
        raise ValueError("Published translations differ from complete API receipts")
    result, packed, mapping = yuris_assets(translated, state["game_dir"])
    if (
        sha(source) != result["archive_sha256"]
        or sha(packed) != result["packed_sha256"]
    ):
        raise ValueError("Source/translated YPF hash mismatch")
    if sha(mapping) != result["display_map_sha256"]:
        raise ValueError("Display mapping hash mismatch")
    before, after = archive_entries(source), archive_entries(packed)
    if [entry["name"] for entry in before] != [entry["name"] for entry in after]:
        raise ValueError("Archive entry list changed")
    scripts = 0
    for old, new in zip(before, after):
        if old["content"][:4] == b"YSTB":
            old_parts = parse_script(old["content"], key, table)[1]
            new_parts = parse_script(new["content"], key, table)[1]
            if old_parts[0] != new_parts[0] or old_parts[3] != new_parts[3]:
                raise ValueError("Game instructions or line table changed")
            if not new_parts[2].startswith(old_parts[2]):
                raise ValueError("Original resources were not retained")
            scripts += 1
        elif old["content"] != new["content"]:
            raise ValueError("Non-script archive entry was modified")
    evidence = {
        "status": "ready_for_game_launch_verification",
        "workflow": state["workflow_id"],
        "source_texts": len(units),
        "selected_texts": len(selected),
        "api_result_texts": len(combined),
        "planned_batches": len(plan),
        "successful_api_leaf_batches": leaf_count,
        "archive_entries": len(before),
        "scripts_control_flow_unchanged": scripts,
        "packed_sha256": sha(packed),
        "content_filter": False,
        "translation_quality_check": False,
    }
    if save_report:
        save_json(workflow_root / "api-and-pack-acceptance.json", evidence)
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workflow_root", type=Path)
    parser.add_argument("--save-report", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            audit(args.workflow_root, save_report=args.save_report), ensure_ascii=True
        )
    )
