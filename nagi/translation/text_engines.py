"""Shared API translation loop for KiriKiri, Ren'Py and TyranoScript."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..gameio.text_engines import SOURCE_KINDS, save_json, sha256_bytes
from .yuris import PROMPT, batches, complete_batch


def translate(job, corpus, client_factory, engine, *, workers=4):
    corpus = Path(corpus)
    extraction = json.loads((corpus / "extraction.json").read_text(encoding="utf-8"))
    all_units = json.loads((corpus / "texts.json").read_text(encoding="utf-8"))
    if extraction.get("engine") != engine or extraction.get("text_count") != len(all_units):
        raise ValueError("提取清单与文本语料不一致，请重新提取")
    if job.mode not in {"partial", "full"}:
        raise ValueError("请选择部分翻译或全文翻译")
    units = all_units[:50] if job.mode == "partial" else all_units
    if not units:
        raise ValueError("所选翻译范围没有文本")
    root = Path(job.output_root)
    root.mkdir(parents=True, exist_ok=True)
    planned = list(batches(units))
    config = job.model_config or {}
    identity = {
        "schema_version": 1, "engine": engine, "family": SOURCE_KINDS[engine],
        "mode": job.mode, "provider": config.get("provider"), "model": config.get("model"),
        "base_url": config.get("base_url"), "protocol": config.get("protocol"),
        "prompt_sha256": sha256_bytes(PROMPT.encode()),
        "units_sha256": sha256_bytes(json.dumps(units, ensure_ascii=False, sort_keys=True).encode()),
        "text_count": len(units), "source_text_count": len(all_units),
        "selected_ids": [unit["id"] for unit in units], "batch_count": len(planned),
    }
    plan_path = root / "translation-plan.json"
    previous = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else None
    if previous and previous != identity:
        raise ValueError("已保存的翻译计划与当前范围/模型不一致，请新建任务")
    save_json(plan_path, identity)
    requests = root / "api-batches"
    requests.mkdir(exist_ok=True)
    label = "部分翻译" if job.mode == "partial" else "全文翻译"
    job.update(
        total_units=len(units), total_batches=len(planned), completed_batches=0,
        translated_units=0, stage="translation", progress=0.36,
        message=f"准备提交 {len(units)} 条 {SOURCE_KINDS[engine]} 文本",
        event=f"{label}：沿用相同 API 批次、费用确认与断点回执流程",
    )
    combined, lock, abort = {}, threading.Lock(), threading.Event()

    def worker(index, batch):
        if abort.is_set() or job.cancel_event.is_set():
            raise InterruptedError()
        translated = complete_batch(job, client_factory, requests, f"{index:05d}", batch)
        with lock:
            combined.update(translated)
            job.update(
                completed_batches=job.completed_batches + 1,
                translated_units=len(combined),
                progress=0.36 + 0.58 * len(combined) / len(units),
                message=f"{label}：{len(combined)}/{len(units)} 条",
            )
            save_json(requests.parent / "progress.json", {
                "completed_batches": job.completed_batches,
                "total_batches": len(planned), "translated_units": len(combined),
                "total_units": len(units),
            })

    failure = None
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"{engine}-api") as executor:
        futures = [executor.submit(worker, index, batch) for index, batch in enumerate(planned)]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                failure = failure or exc
                abort.set()
        if job.cancel_event.is_set():
            raise InterruptedError()
        if failure:
            raise failure
    save_json(root / "translations.json", combined)
    save_json(root / "translation-result.json", {
        **identity, "status": "completed", "translated_count": len(combined),
        "translations_sha256": sha256_bytes((root / "translations.json").read_bytes()),
    })
    job.update(
        status="completed", progress=1, stage="output",
        translated_scripts_dir=str(root),
        message=f"{label} {len(combined)} 条完成，可生成独立汉化启动文件",
        event="API 原始返回与译文已保存；脚本回写将在第三步独立副本中完成",
    )
