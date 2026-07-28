from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

from dashcam.recorder.gstreamer import (
    EffectiveCaps,
    EncoderIdentity,
    FinalizedFragment,
    FrameCounters,
    OpenedFragment,
    SegmentedOutputConfig,
)
from dashcam.recorder.pipeline import RecoverablePipelineError, VideoProfile

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "deploy/ssh-dev-validation/milestone6-recovery/run.py"
README_PATH = HARNESS_PATH.with_name("README.md")
MANIFEST_PATH = HARNESS_PATH.with_name("SHA256SUMS")


def _load() -> ModuleType:
    name = "pi_m6_recovery_harness"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeBackend:
    def __init__(self) -> None:
        self.encoded = 0
        self.started = False
        self.run_cancelled = False
        self.stopped = False
        self.opened = OpenedFragment(Path("/srv/dashcam/pending/fake.mp4"), 7, 0)
        self.effective_caps = EffectiveCaps(
            1920,
            1080,
            30,
            1,
            "NV12",
            "h264",
            "high",
            "4.1",
        )
        self.encoder_identity = EncoderIdentity(
            "v4l2h264enc",
            "Codec/Encoder/Video/Hardware",
            "/dev/video11",
        )

    def frame_counters(self) -> FrameCounters:
        return FrameCounters(self.encoded, self.encoded, 0, "pts_discontinuity")

    async def start(self, requested_profile: VideoProfile) -> VideoProfile:
        self.started = True
        return requested_profile

    async def run(self, stop_requested: asyncio.Event) -> None:
        del stop_requested
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.run_cancelled = True
            raise

    async def stop(self) -> None:
        self.stopped = True

    async def wait_for_first_fragment_opened(self) -> OpenedFragment:
        return self.opened

    async def next_finalized_fragment(self) -> FinalizedFragment:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    def mark_finalized_fragment_processed(self) -> None:
        return None

    async def wait_for_finalized_fragments_processed(self) -> None:
        return None


def test_wrapper_injects_once_after_open_time_and_frames_then_delegates_stop() -> None:
    harness = _load()
    inner = FakeBackend()
    clock = iter((0.0, 5.0, 5.0))

    async def advance(_delay: float) -> None:
        inner.encoded = 150
        await asyncio.sleep(0)

    wrapper = harness.InjectedRecoveryBackend(
        inner,
        injection_seconds=5,
        injection_frames=150,
        monotonic=lambda: next(clock),
        sleep=advance,
    )

    async def exercise() -> None:
        await wrapper.start(VideoProfile())
        with pytest.raises(RecoverablePipelineError, match="reviewed exact-Pi"):
            await wrapper.run(asyncio.Event())
        await wrapper.stop()

    asyncio.run(exercise())

    assert inner.started
    assert inner.run_cancelled
    assert inner.stopped
    assert wrapper.injected
    assert wrapper.inner_run_joined
    assert wrapper.stop_delegated
    assert wrapper.elapsed_at_injection_s == 5
    assert wrapper.encoded_delta_at_injection == 150
    assert wrapper.effective_caps is inner.effective_caps
    assert wrapper.encoder_identity is inner.encoder_identity


def test_factory_wraps_only_first_backend_and_returns_replacements_unmodified(
    tmp_path: Path,
) -> None:
    harness = _load()
    made: list[FakeBackend] = []

    def build(_output: SegmentedOutputConfig) -> FakeBackend:
        backend = FakeBackend()
        made.append(backend)
        return backend

    factory = harness.FirstAttemptRecoveryFactory(build)
    del tmp_path
    pending = Path("/srv/dashcam/pending")
    first_output = SegmentedOutputConfig(pending, "abcdef123456", start_index=1)
    second_output = SegmentedOutputConfig(pending, "abcdef123456", start_index=2)

    first = factory(first_output)
    second = factory(second_output)

    assert isinstance(first, harness.InjectedRecoveryBackend)
    assert second is made[1]
    assert not isinstance(second, harness.InjectedRecoveryBackend)
    assert factory.outputs == [first_output, second_output]
    assert factory.backends == made


def test_inactive_proof_is_closed_hashed_current_and_inactive(tmp_path: Path) -> None:
    harness = _load()
    proof = {
        "schema_version": 1,
        "unit": "dashcamd.service",
        "active_state": "inactive",
        "sub_state": "dead",
        "main_pid": 0,
        "boot_id": "601693e3-fa96-427e-906b-1621463a15cd",
        "observed_monotonic_ns": 10_000_000_000,
    }
    path = tmp_path / "proof.json"
    payload = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    parsed = harness.verify_inactive_proof(
        path,
        digest,
        boot_id=proof["boot_id"],
        monotonic_ns=11_000_000_000,
        require_root_owner=False,
    )

    assert parsed == proof
    proof["extra"] = True
    changed = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(changed)
    with pytest.raises(harness.HarnessError, match="schema"):
        harness.verify_inactive_proof(
            path,
            hashlib.sha256(changed).hexdigest(),
            boot_id="601693e3-fa96-427e-906b-1621463a15cd",
            monotonic_ns=11_000_000_000,
            require_root_owner=False,
        )


def test_inactive_proof_rejects_active_stale_and_wrong_hash(tmp_path: Path) -> None:
    harness = _load()
    proof: dict[str, Any] = {
        "schema_version": 1,
        "unit": "dashcamd.service",
        "active_state": "active",
        "sub_state": "running",
        "main_pid": 42,
        "boot_id": "601693e3-fa96-427e-906b-1621463a15cd",
        "observed_monotonic_ns": 1,
    }
    path = tmp_path / "proof.json"
    payload = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    with pytest.raises(harness.HarnessError, match="does not prove"):
        harness.verify_inactive_proof(
            path,
            digest,
            boot_id=proof["boot_id"],
            monotonic_ns=2,
            require_root_owner=False,
        )
    proof.update(active_state="inactive", sub_state="dead", main_pid=0)
    payload = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(payload)
    with pytest.raises(harness.HarnessError, match="hash differs"):
        harness.verify_inactive_proof(
            path,
            "0" * 64,
            boot_id=proof["boot_id"],
            monotonic_ns=2,
            require_root_owner=False,
        )
    with pytest.raises(harness.HarnessError, match="stale"):
        harness.verify_inactive_proof(
            path,
            hashlib.sha256(payload).hexdigest(),
            boot_id=proof["boot_id"],
            monotonic_ns=200_000_000_000,
            require_root_owner=False,
        )


def test_manifest_is_closed_to_readme_and_script(tmp_path: Path) -> None:
    harness = _load()
    (tmp_path / "README.md").write_text("reviewed\n", encoding="utf-8")
    (tmp_path / "run.py").write_text("print('reviewed')\n", encoding="utf-8")
    lines = [
        f"{hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}  {name}"
        for name in ("README.md", "run.py")
    ]
    manifest = ("\n".join(lines) + "\n").encode()
    (tmp_path / "SHA256SUMS").write_bytes(manifest)

    assert harness.verify_manifest(
        hashlib.sha256(manifest).hexdigest(), tmp_path
    ) == {
        name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        for name in ("README.md", "run.py")
    }
    (tmp_path / "extra").write_text("unreviewed", encoding="utf-8")
    extra_hash = hashlib.sha256((tmp_path / "extra").read_bytes()).hexdigest()
    changed = manifest + f"{extra_hash}  extra\n".encode()
    (tmp_path / "SHA256SUMS").write_bytes(changed)
    with pytest.raises(harness.HarnessError, match="not closed"):
        harness.verify_manifest(hashlib.sha256(changed).hexdigest(), tmp_path)


def test_release_provenance_retains_venv_symlink_path_with_outside_target(
    tmp_path: Path,
) -> None:
    harness = _load()
    releases = (tmp_path / "releases").resolve()
    release = releases / "release-123"
    venv = release / "venv"
    executable = venv / "bin/python"
    module = release / "lib/python3.13/site-packages/dashcam/__init__.py"
    target = tmp_path / "system/usr/bin/python3.13"
    module.parent.mkdir(parents=True)
    module.write_text("# installed package\n", encoding="utf-8")
    executable.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"outside-system-interpreter")
    symlink_info = os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    observed = harness.verify_release_layout(
        executable=executable,
        prefix=venv,
        module_path=module,
        releases_root=releases,
        executable_lstat=lambda _path: symlink_info,
        executable_realpath=lambda _path: str(target),
    )

    assert observed["interpreter"] == executable.as_posix()
    assert observed["interpreter_target"] == target.as_posix()
    assert observed["venv_prefix"] == venv.as_posix()
    assert observed["release"] == "release-123"


def test_release_provenance_rejects_prefix_or_package_from_another_release(
    tmp_path: Path,
) -> None:
    harness = _load()
    releases = (tmp_path / "releases").resolve()
    release = releases / "release-123"
    other = releases / "release-456"
    executable = release / "venv/bin/python"
    module = release / "site-packages/dashcam/__init__.py"
    other_module = other / "site-packages/dashcam/__init__.py"
    for path in (executable, module, other_module):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    with pytest.raises(harness.HarnessError, match=r"sys\.prefix"):
        harness.verify_release_layout(
            executable=executable,
            prefix=other / "venv",
            module_path=module,
            releases_root=releases,
        )
    with pytest.raises(harness.HarnessError, match="importing"):
        harness.verify_release_layout(
            executable=executable,
            prefix=release / "venv",
            module_path=other_module,
            releases_root=releases,
        )


def test_wait_until_fails_immediately_with_terminal_daemon_result() -> None:
    harness = _load()

    async def exercise() -> Any:
        async def finish() -> object:
            return SimpleNamespace(
                outcome=SimpleNamespace(value="STARTUP_FAILED"),
                final_status=SimpleNamespace(
                    as_dict=lambda: {
                        "state": "FAULTED",
                        "reason": "STARTUP_FAILED",
                    }
                ),
            )

        task = asyncio.create_task(finish())
        await task
        daemon_evidence = harness._daemon_task_diagnostic(task)
        with pytest.raises(harness.ScenarioFailure) as caught:
            await harness._wait_until(
                lambda: False,
                deadline=harness.time.monotonic() + 60,
                detail="replacement missing",
                terminal=task.done,
                diagnostics=lambda: {"daemon_task": daemon_evidence},
            )
        return caught.value

    failure = asyncio.run(exercise())

    assert "terminated first" in str(failure)
    assert failure.diagnostics["daemon_task"]["state"] == "finished"
    assert (
        failure.diagnostics["daemon_task"]["result"]["outcome"]
        == "STARTUP_FAILED"
    )
    assert (
        failure.diagnostics["daemon_task"]["result"]["final_status"]["reason"]
        == "STARTUP_FAILED"
    )


def test_daemon_task_diagnostic_preserves_bounded_exception() -> None:
    harness = _load()

    async def exercise() -> dict[str, object]:
        async def fail() -> None:
            raise RuntimeError("camera startup refused")

        task = asyncio.create_task(fail())
        with pytest.raises(RuntimeError):
            await task
        return cast(dict[str, object], harness._daemon_task_diagnostic(task))

    observed = asyncio.run(exercise())

    assert observed["state"] == "failed"
    assert observed["result"] is None
    assert observed["error"] == "RuntimeError: camera startup refused"


def test_source_has_no_service_network_storage_or_media_removal_mutators() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8").casefold()
    forbidden = (
        "systemctl",
        "nmcli",
        "hostapd",
        "mkfs",
        "sfdisk",
        "parted",
        "wipefs",
        "shutil.rmtree",
        ".unlink(",
        "os.remove",
        "os.rename",
        "subprocess",
        "pipeline_description",
        "video_bitrate_mode",
    )
    for token in forbidden:
        assert token not in source
    assert "--config" not in source
    assert "--identity" not in source
    assert "os.o_excl" in source


def test_checked_bundle_manifest_matches_current_bytes() -> None:
    entries = {}
    for line in MANIFEST_PATH.read_text(encoding="ascii").splitlines():
        digest, name = line.split("  ")
        entries[name] = digest
    assert set(entries) == {"README.md", "run.py"}
    assert entries["README.md"] == hashlib.sha256(README_PATH.read_bytes()).hexdigest()
    assert entries["run.py"] == hashlib.sha256(HARNESS_PATH.read_bytes()).hexdigest()
