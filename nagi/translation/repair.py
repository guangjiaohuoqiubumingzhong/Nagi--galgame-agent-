"""User-triggered retranslation of source-identical Japanese records.

This is deliberately not an automatic quality gate.  A normal translation run
keeps its existing transport-only response contract.  Repair runs happen only
after the user explicitly selects the workbench's ``repair`` action.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4

from ..gameio.yuris import save_json
from .yuris import batches, complete_batch

KANA = re.compile(r"[\u3040-\u30ff]")


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def untranslated_records(units, translations):
    """Select only exact source copies containing Japanese kana.

    Equality makes the user-requested action deterministic and auditable.  It
    does not attempt to score fluency, terminology, style, or mixed-language
    output and is never called by the ordinary translation/deployment path.
    """
    expected = {unit["id"] for unit in units}
    if set(translations) != expected:
        raise ValueError("已有全文译文与提取文本的编号不一致，不能查缺补漏")
    return [
        {"id": unit["id"], "text": unit["text"]}
        for unit in units
        if translations[unit["id"]] == unit["text"] and KANA.search(unit["text"])
    ]


def _active_run(root, units, translations, engine, config):
    root = Path(root)
    # Keep the run directly below the full result.  Windows game/workflow paths
    # are often already long; another nested audit directory can exceed the
    # legacy 260-character boundary when durable receipt temp names are added.
    runs = root
    active_path = root / "repair-active.json"
    baseline_sha = file_sha256(root / "translations.json")
    selected = untranslated_records(units, translations)
    if active_path.exists():
        active = json.loads(active_path.read_text(encoding="utf-8"))
        relative = active.get("run")
        run = (root / relative).resolve() if isinstance(relative, str) else root
        if (
            root.resolve() not in run.parents
            or run.parent != root.resolve()
            or not run.name.startswith(".repair-")
            or not run.is_dir()
        ):
            raise ValueError("查缺补漏断点目录无效")
        result_path = run / "repair-result.json"
        if result_path.exists():
            active_path.unlink()
        else:
            plan = json.loads((run / "repair-plan.json").read_text(encoding="utf-8"))
            if (
                plan.get("schema_version") != 1
                or plan.get("engine") != engine
                or plan.get("baseline_translations_sha256") != baseline_sha
                or plan.get("provider") != config.get("provider")
                or plan.get("model") != config.get("model")
                or plan.get("selected_ids") != [unit["id"] for unit in selected]
            ):
                raise ValueError("查缺补漏断点与当前全文译文或模型不一致")
            return run, selected, plan
    run = runs / (".repair-" + uuid4().hex[:8])
    run.mkdir()
    plan = {
        "schema_version": 1,
        "action": "repair",
        "engine": engine,
        "provider": config.get("provider"),
        "model": config.get("model"),
        "baseline_translations_sha256": baseline_sha,
        "selected_ids": [unit["id"] for unit in selected],
        "selected_count": len(selected),
        "batch_count": len(list(batches(selected))),
    }
    save_json(run / "repair-plan.json", plan)
    save_json(active_path, {"schema_version": 1, "run": str(run.relative_to(root))})
    return run, selected, plan


def repair_text_result(
    job,
    root,
    units,
    client_factory,
    *,
    engine,
    glossary=None,
    workers=4,
):
    """Retranslate selected records and atomically merge them into a full result."""
    root = Path(root)
    translations_path = root / "translations.json"
    result_path = root / "translation-result.json"
    if not translations_path.is_file() or not result_path.is_file():
        raise ValueError("请先完成一次全文翻译，再使用查缺补漏")
    translations = json.loads(translations_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if (
        result.get("status") != "completed"
        or result.get("translated_count") != len(units)
        or result.get("translations_sha256") != file_sha256(translations_path)
    ):
        raise ValueError("已有全文翻译结果不完整或已改变，不能查缺补漏")
    config = job.model_config or {}
    run, selected, plan = _active_run(root, units, translations, engine, config)
    planned = list(batches(selected))
    requests = run / "api-batches"
    requests.mkdir(exist_ok=True)
    job.update(
        total_units=len(selected),
        total_batches=len(planned),
        completed_batches=0,
        translated_units=0,
        stage="translation",
        progress=0.36,
        message=(
            f"查缺补漏：准备重新翻译 {len(selected)} 条内容"
            if selected
            else "查缺补漏：没有需要重新翻译的内容"
        ),
        event=f"查缺补漏：选出 {len(selected)} 条意外未翻译内容",
    )
    repaired, lock, abort = {}, threading.Lock(), threading.Event()

    def worker(index, batch):
        if abort.is_set() or job.cancel_event.is_set():
            raise InterruptedError()
        translated = complete_batch(
            job,
            client_factory,
            requests,
            f"{index:05d}",
            batch,
            glossary=glossary,
        )
        with lock:
            repaired.update(translated)
            job.update(
                completed_batches=job.completed_batches + 1,
                translated_units=len(repaired),
                progress=0.36 + 0.58 * len(repaired) / len(selected),
                message=f"查缺补漏：{len(repaired)}/{len(selected)} 条",
            )
            save_json(
                run / "progress.json",
                {
                    "completed_batches": job.completed_batches,
                    "total_batches": len(planned),
                    "translated_units": len(repaired),
                    "total_units": len(selected),
                },
            )

    failure = None
    if planned:
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="translation-repair"
        ) as executor:
            futures = [
                executor.submit(worker, index, batch)
                for index, batch in enumerate(planned)
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 - preserve resumable receipts
                    failure = failure or exc
                    abort.set()
            if job.cancel_event.is_set():
                raise InterruptedError()
            if failure:
                raise failure
    if file_sha256(translations_path) != plan["baseline_translations_sha256"]:
        raise ValueError("查缺补漏期间全文译文发生变化，停止合并")
    shutil.copyfile(translations_path, run / "translations.before.json")
    shutil.copyfile(result_path, run / "translation-result.before.json")
    merged = dict(translations)
    merged.update(repaired)
    history = list(result.get("repair_history", []))
    history.append(
        {
            "run": str(run.relative_to(root)),
            "selected_count": len(selected),
            "batch_count": len(planned),
            "provider": config.get("provider"),
            "model": config.get("model"),
        }
    )
    try:
        save_json(translations_path, merged)
        updated_result = {
            **result,
            "translations_sha256": file_sha256(translations_path),
            "repair_history": history,
        }
        save_json(result_path, updated_result)
        save_json(
            run / "repair-result.json",
            {
                **plan,
                "status": "completed",
                "merged_count": len(repaired),
                "translations_sha256": updated_result["translations_sha256"],
            },
        )
    except Exception:
        # Keep the previously deployable full result intact if publishing the
        # merged pair or its audit receipt is interrupted.
        shutil.copyfile(run / "translations.before.json", translations_path)
        shutil.copyfile(run / "translation-result.before.json", result_path)
        raise
    (root / "repair-active.json").unlink(missing_ok=True)
    job.update(
        status="completed",
        progress=1,
        stage="output",
        translated_scripts_dir=str(root),
        message=f"查缺补漏完成，已重新翻译 {len(repaired)} 条内容",
        event="补翻回包已合并到原全文结果；原有其他译文保持不变",
    )
    return repaired
