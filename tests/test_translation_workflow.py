import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib import error, request

import pytest

from nagi.translation_workflow import TranslationWorkflow, TranslationWorkflows
from nagi.webapp import QlieWebServer, TranslationJob


def settle(item):
    deadline = time.monotonic() + 3
    while item.active and time.monotonic() < deadline:
        time.sleep(0.005)
    assert item.active is None
    return item.public_dict()


@pytest.fixture
def workflow(tmp_path):
    game, storage = tmp_path / "game", tmp_path / "chosen-output"
    game.mkdir()
    storage.mkdir()
    (game / "original.txt").write_text("original", encoding="utf-8")
    calls = []

    def prepare(job, assessment):
        assert assessment["prepared"] is None
        calls.append("extract")
        root = Path(job.output_root)
        export, corpus = root / "script-export", root / "corpus"
        export.mkdir(parents=True, exist_ok=True)
        corpus.mkdir(exist_ok=True)
        job.update(export_dir=str(export), corpus_dir=str(corpus), progress=0.34)
        return export, corpus

    def translate(job, export, corpus):
        calls.append(("translate", job.mode))
        assert export.is_dir() and corpus.is_dir()
        output = Path(job.output_root) / "translated-scripts"
        output.mkdir(parents=True)
        job.update(
            status="completed",
            progress=1,
            translated_scripts_dir=str(output),
            message="翻译完成",
        )

    def config():
        calls.append("config")
        return {
            "provider": "fixture",
            "model": "fixture-model",
            "api_key": "private-test-key",
        }

    backend = SimpleNamespace(
        TranslationJob=TranslationJob,
        _configured_model=config,
        assess_game_directory=lambda path: {
            "supported_archive_count": 1,
            "key_available": True,
        },
        _prepare_source=prepare,
        _translate_and_publish=translate,
    )
    return TranslationWorkflows(backend), game, storage, calls


def test_extract_uses_chosen_storage_and_never_loads_model(workflow):
    service, game, storage, calls = workflow
    item = service.create(str(game), str(storage))
    payload = settle(item)
    assert payload["stages"]["extract"]["status"] == "completed"
    assert payload["stages"]["translate"]["status"] == "pending"
    assert calls == ["extract"]
    assert storage in Path(payload["export_dir"]).parents
    assert (game / "original.txt").read_text() == "original"


@pytest.mark.parametrize("stage", ["translate", "deploy"])
def test_backend_rejects_skipping_extraction(workflow, stage):
    service, game, storage, calls = workflow
    item = TranslationWorkflow("empty", game, storage)
    service.items["empty"] = item
    with pytest.raises(ValueError, match="第一步"):
        service.start("empty", stage, confirmed=True)
    assert calls == []


def test_requires_translation_before_deployment_and_cost_consent(workflow):
    service, game, storage, calls = workflow
    item = service.create(str(game), str(storage))
    settle(item)
    with pytest.raises(ValueError, match="第二步"):
        service.start(item.workflow_id, "deploy")
    with pytest.raises(ValueError, match="费用"):
        service.start(item.workflow_id, "translate")
    assert calls == ["extract"]


def test_repair_requires_supported_engine_and_completed_full_result(workflow):
    service, game, storage, calls = workflow
    item = service.create(str(game), str(storage))
    settle(item)
    with pytest.raises(ValueError, match="暂不支持"):
        service.start(item.workflow_id, "translate", mode="repair", confirmed=True)
    assert calls == ["extract"]


def test_pilot_then_full_are_independent_and_clear_previous_deployment(workflow):
    service, game, storage, calls = workflow
    item = service.create(str(game), str(storage))
    settle(item)
    for mode in ["pilot", "partial", "full"]:
        service.start(item.workflow_id, "translate", mode=mode, confirmed=True)
        payload = settle(item)
        assert payload["stages"]["translate"]["progress"] == 1
        assert payload["mode"] == mode
        assert "private-test-key" not in json.dumps(payload)
        assert payload["stages"]["deploy"]["status"] == "pending"
    assert calls.count("extract") == 1
    assert ("translate", "pilot") in calls and ("translate", "full") in calls
    assert ("translate", "partial") in calls


def test_no_fake_launcher_when_engine_deployment_is_not_integrated(workflow):
    service, game, storage, _calls = workflow
    item = service.create(str(game), str(storage))
    settle(item)
    service.start(item.workflow_id, "translate", confirmed=True)
    settle(item)
    service.start(item.workflow_id, "deploy")
    payload = settle(item)
    assert payload["stages"]["deploy"]["status"] == "failed"
    assert "尚未接入" in payload["stages"]["deploy"]["error"]
    assert payload["launcher_path"] is None
    assert payload["stages"]["translate"]["status"] == "completed"
    assert Path(payload["translated_scripts_dir"]).is_dir()


def test_deployment_adapter_has_progress_and_must_return_real_output(workflow):
    service, game, storage, _calls = workflow
    gate, entered = threading.Event(), threading.Event()

    def deploy(item, progress):
        progress(0.5, "正在生成独立启动入口")
        entered.set()
        assert gate.wait(2)
        launcher = Path(item.playable_root) / "fixture-launcher.ps1"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("# synthetic test, no game launched", encoding="utf-8")
        return launcher

    service.deployer = deploy
    item = service.create(str(game), str(storage))
    settle(item)
    service.start(item.workflow_id, "translate", confirmed=True)
    settle(item)
    service.start(item.workflow_id, "deploy")
    try:
        assert entered.wait(2)
        assert item.public_dict()["stages"]["deploy"]["progress"] == 0.5
        with pytest.raises(ValueError, match="执行"):
            service.start(item.workflow_id, "translate", confirmed=True)
    finally:
        gate.set()
    payload = settle(item)
    assert payload["stages"]["deploy"]["status"] == "completed"
    assert Path(payload["launcher_path"]).is_file()
    assert Path(payload["launcher_path"]).parent == Path(item.playable_root).parent
    assert "default_launcher_path" not in payload
    assert not (Path(item.playable_root).parent / "启动汉化版.cmd").exists()


def test_source_output_overlap_and_empty_paths_rejected(workflow):
    service, game, storage, _calls = workflow
    nested = game / "nested"
    nested.mkdir()
    for target in [game, nested]:
        with pytest.raises(ValueError, match="原始游戏目录"):
            service.create(str(game), str(target))
    with pytest.raises(ValueError, match="原始游戏"):
        service.create("", str(storage))
    with pytest.raises(ValueError, match="保存目录"):
        service.create(str(game), "")
    assert not service.items


def test_review_and_cancel_do_not_unlock_translation(workflow):
    service, game, storage, _calls = workflow

    def review(job, assessment):
        job.update(
            status="review_required", message="需要审核", review_path="review.json"
        )
        return None, None

    service.backend._prepare_source = review
    item = service.create(str(game), str(storage))
    assert settle(item)["stages"]["extract"]["status"] == "review_required"
    with pytest.raises(ValueError, match="第一步"):
        service.start(item.workflow_id, "translate", confirmed=True)


def test_translation_failure_reuses_checkpoint_and_original_model(workflow):
    service, game, storage, _calls = workflow
    seen = []

    def fail(job, *_args):
        seen.append(job)
        job.update(progress=0.6)
        raise RuntimeError("fixture failure")

    service.backend._translate_and_publish = fail
    item = service.create(str(game), str(storage))
    settle(item)
    service.start(item.workflow_id, "translate", confirmed=True)
    payload = settle(item)
    assert payload["stages"]["translate"]["status"] == "failed"
    assert payload["stages"]["translate"]["progress"] > 0
    service.backend._configured_model = lambda: pytest.fail(
        "resume must retain model snapshot"
    )
    service.start(item.workflow_id, "translate", confirmed=True)
    settle(item)
    assert seen[0] is seen[1]


def test_workflow_http_routes_enforce_order_and_return_selected_storage(workflow):
    service, game, storage, _calls = workflow
    server = QlieWebServer(("127.0.0.1", 0))
    server.translation_workflows = service
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path, data):
        data = {**data, "csrf_token": server.csrf_token}
        req = request.Request(
            base + path,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
        )
        with request.urlopen(req) as response:
            return json.load(response)

    try:
        with pytest.raises(error.HTTPError) as exc:
            post("/api/translation-workflows/missing/deploy", {})
        assert exc.value.code == 400
        payload = post(
            "/api/translation-workflows",
            {"game_dir": str(game), "storage_dir": str(storage)},
        )
        identifier = payload["workflow_id"]
        settle(service.get(identifier))
        with request.urlopen(
            base + "/api/translation-workflows/" + identifier
        ) as response:
            assert json.load(response)["storage_dir"] == str(storage)
        with pytest.raises(error.HTTPError) as exc:
            post(f"/api/translation-workflows/{identifier}/deploy", {})
        assert "第二步" in json.load(exc.value)["error"]
        with request.urlopen(base + "/translation-workflow.js") as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
