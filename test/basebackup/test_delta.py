# Copyright (c) 2022 Aiven, Helsinki, Finland. https://aiven.io/
from pathlib import Path
from queue import Empty
from test.base import CONSTANT_TEST_RSA_PUBLIC_KEY
from typing import Any, Dict
from unittest.mock import MagicMock, Mock

import pytest
from mock import patch
from rohmu.delta.common import SnapshotFile, SnapshotResult, SnapshotState
from rohmu.delta.snapshot import Snapshotter
from rohmu.errors import FileNotFoundFromStorageError

from pghoard.basebackup import ChunkUploader, DeltaBaseBackup
from pghoard.basebackup.delta import UploadedFilesMetric
from pghoard.common import (BackupFailure, BaseBackupFormat, CallbackQueue, CompressionData, EncryptionData)
from pghoard.metrics import Metrics
from pghoard.transfer import TransferQueue


@pytest.fixture(name="delta_paths")
def fixture_delta_paths(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    yield src, dst


@pytest.fixture(name="snapshotter")
def fixture_snapshotter(delta_paths):
    src, dst = delta_paths
    return Snapshotter(globs=["**/*"], src=src, dst=dst)


@pytest.fixture(name="delta_file")
def fixture_delta_file(delta_paths):
    src, _ = delta_paths
    file_name = "test.dat"

    with open(src / file_name, "w") as f:
        f.write("a" * 200)

    file_hash = "2b033f9f5ba9cf20671da79e492f41545e673b562603945ffed09662fd92321a"

    return file_name, file_hash


@pytest.fixture(name="deltabasebackup")
def fixture_deltabasebackup(tmp_path):
    transfer_queue = TransferQueue()
    metrics = Metrics(statsd={})
    site_config = {
        "prefix": "abc",
    }
    encryption_data = EncryptionData("test_key", CONSTANT_TEST_RSA_PUBLIC_KEY)
    compression_data = CompressionData("snappy", 0)
    chunk_uploader = ChunkUploader(
        metrics=metrics,
        chunks_on_disk=0,
        encryption_data=encryption_data,
        compression_data=compression_data,
        site_config=site_config,
        site="delta",
        is_running=lambda: True,
        transfer_queue=transfer_queue
    )
    storage = Mock()
    data_file_format = "{}/{}.{{0:08d}}.pghoard".format(tmp_path, "test").format
    delta_base_backup = DeltaBaseBackup(
        storage=storage,
        site="delta",
        site_config=site_config,
        transfer_queue=transfer_queue,
        metrics=metrics,
        encryption_data=encryption_data,
        compression_data=compression_data,
        get_remote_basebackups_info=MagicMock(),
        parallel=1,
        temp_base_dir=tmp_path,
        compressed_base=tmp_path,
        chunk_uploader=chunk_uploader,
        data_file_format=data_file_format,
    )

    return delta_base_backup


def generate_backup_meta_sample(base_backup_format: BaseBackupFormat) -> Dict[str, Any]:
    meta: Dict[str, Any] = {"manifest": {"snapshot_result": {"state": {"files": []}}}}
    files = []
    if base_backup_format == BaseBackupFormat.delta_v1:
        files = [{
            "relative_path": "base/1/1",
            "file_size": 8192,
            "stored_file_size": 100,
            "mtime_ns": 1652175599798812244,
            "hexdigest": "delta1hex1",
            "content_b64": None,
        }, {
            "relative_path": "base/1/2",
            "file_size": 8192,
            "stored_file_size": 200,
            "mtime_ns": 1652175599798812244,
            "hexdigest": "delta1hex2",
            "content_b64": None,
        }, {
            "relative_path": "base/1/3",
            "file_size": 1,
            "stored_file_size": 0,
            "mtime_ns": 1652175599798812244,
            "hexdigest": "",
            "content_b64": "b64",
        }]
    elif base_backup_format == BaseBackupFormat.delta_v2:
        files = [{
            "relative_path": "base/1/3",
            "file_size": 8192,
            "stored_file_size": 50,
            "mtime_ns": 1652175599798812244,
            "hexdigest": "delta2hex1",
            "content_b64": None,
            "should_be_bundled": False
        }, {
            "relative_path": "base/1/4",
            "file_size": 8192,
            "stored_file_size": 150,
            "mtime_ns": 1652175599798812244,
            "hexdigest": "delta2hex2",
            "content_b64": None,
            "should_be_bundled": False
        }, {
            "relative_path": "base/1/4",
            "file_size": 8192,
            "stored_file_size": 0,
            "mtime_ns": 1652175599798812244,
            "hexdigest": "",
            "content_b64": "b64",
            "should_be_bundled": False,
        }]

    meta["manifest"]["snapshot_result"]["state"]["files"] = files
    return meta


def fake_download_backup_meta(basebackup_path: str, **kwargs):  # pylint: disable=unused-argument
    meta = {}
    if basebackup_path == "abc/basebackup/delta_v1":
        meta = generate_backup_meta_sample(BaseBackupFormat.delta_v1)
    elif basebackup_path == "abc/basebackup/delta_v2":
        meta = generate_backup_meta_sample(BaseBackupFormat.delta_v2)

    return meta, b"abc"


class TestDeltaBaseBackup:
    def test_list_existing_files_skips_non_delta_formats(self, deltabasebackup):
        with patch.object(deltabasebackup, "get_remote_basebackups_info") as mock_get_remote_basebackups_info, \
                patch("pghoard.basebackup.delta.download_backup_meta_file", new=fake_download_backup_meta):
            mock_get_remote_basebackups_info.return_value = [
                {
                    "metadata": {
                        "format": BaseBackupFormat.v1
                    }
                },
                {
                    "metadata": {
                        "format": BaseBackupFormat.v2
                    }
                },
                {
                    "metadata": {
                        "format": BaseBackupFormat.delta_v1
                    },
                    "name": "delta_v1"
                },
                {
                    "metadata": {
                        "format": BaseBackupFormat.delta_v2
                    },
                    "name": "delta_v2"
                },
                {
                    "metadata": {
                        "format": "unknown"
                    }
                },
            ]
            assert deltabasebackup._list_existing_files() == {  # pylint: disable=protected-access
                "delta1hex1": SnapshotFile(
                    relative_path=Path("base/1/1"),
                    file_size=8192,
                    stored_file_size=100,
                    mtime_ns=1652175599798812244,
                    hexdigest="delta1hex1",
                    content_b64=None
                ),
                "delta1hex2": SnapshotFile(
                    relative_path=Path("base/1/2"),
                    file_size=8192,
                    stored_file_size=200,
                    mtime_ns=1652175599798812244,
                    hexdigest="delta1hex2",
                    content_b64=None
                ),
                "delta2hex1": SnapshotFile(
                    relative_path=Path("base/1/3"),
                    file_size=8192,
                    stored_file_size=50,
                    mtime_ns=1652175599798812244,
                    hexdigest="delta2hex1",
                    content_b64=None
                ),
                "delta2hex2": SnapshotFile(
                    relative_path=Path("base/1/4"),
                    file_size=8192,
                    stored_file_size=150,
                    mtime_ns=1652175599798812244,
                    hexdigest="delta2hex2",
                    content_b64=None
                )
            }

    @pytest.mark.parametrize("chunk_size", [0, 1, 2, 3])
    def test_split_files_for_upload_bundled_files(self, chunk_size):
        bundled_file_1 = SnapshotFile(
            relative_path=Path("file1"),
            file_size=1,
            stored_file_size=1,
            mtime_ns=0,
            should_be_bundled=True,
            missing_ok=False
        )
        bundled_file_2 = SnapshotFile(
            relative_path=Path("file2"),
            file_size=1,
            stored_file_size=1,
            mtime_ns=0,
            should_be_bundled=True,
            missing_ok=True,
        )
        bundled_file_3 = SnapshotFile(
            relative_path=Path("file3"),
            file_size=1,
            stored_file_size=1,
            mtime_ns=0,
            should_be_bundled=True,
            hexdigest="xyz",
            missing_ok=True
        )
        files = [bundled_file_1, bundled_file_2, bundled_file_3]
        snapshot_result = SnapshotResult(state=SnapshotState(root_globs=["**/*"], files=files, empty_dirs=[]))
        delta_chunks, hexdigests = DeltaBaseBackup._split_files_for_upload(  # pylint: disable=protected-access
            snapshot_result=snapshot_result, snapshot_dir=Path("/dir"), chunk_size=chunk_size
        )
        assert not hexdigests
        chunk_file1 = (Path("file1"), Path("/dir/file1"), False)
        chunk_file2 = (Path("file2"), Path("/dir/file2"), True)
        chunk_file3 = (Path("file3"), Path("/dir/file3"), True)
        if chunk_size <= 1:
            assert delta_chunks == [{chunk_file1}, {chunk_file2}, {chunk_file3}]
        elif chunk_size == 2:
            assert delta_chunks == [{chunk_file1, chunk_file2}, {chunk_file3}]
        elif chunk_size == 3:
            assert delta_chunks == [{chunk_file1, chunk_file2, chunk_file3}]

    def test_split_files_for_upload_mixed_files(self):
        hexdigest_file = SnapshotFile(
            relative_path=Path("file1"), file_size=5, stored_file_size=1, mtime_ns=0, hexdigest="abc"
        )
        bundled_file_1 = SnapshotFile(
            relative_path=Path("file2"), file_size=2, stored_file_size=1, mtime_ns=0, should_be_bundled=True
        )
        bundled_file_2 = SnapshotFile(
            relative_path=Path("file3"),
            file_size=2,
            stored_file_size=1,
            mtime_ns=0,
            should_be_bundled=True,
            hexdigest="xyz"
        )
        _ = SnapshotFile(relative_path=Path("file3"), file_size=2, stored_file_size=1, mtime_ns=0, content_b64="b64==")
        files = [hexdigest_file, bundled_file_1, bundled_file_2]
        snapshot_result = SnapshotResult(state=SnapshotState(root_globs=["**/*"], files=files, empty_dirs=[]))
        delta_chunks, hexdigests = DeltaBaseBackup._split_files_for_upload(  # pylint: disable=protected-access
            snapshot_result=snapshot_result, snapshot_dir=Path("/dir"), chunk_size=1
        )
        assert hexdigests == {"abc"}
        chunk_file1 = (Path("file2"), Path("/dir/file2"), True)
        chunk_file2 = (Path("file3"), Path("/dir/file3"), True)
        assert delta_chunks == [{chunk_file1}, {chunk_file2}]

    def test_split_files_for_upload_skips_hexdigests(self):
        hexdigest_file_1 = SnapshotFile(
            relative_path=Path("file1"), file_size=5, stored_file_size=1, mtime_ns=0, hexdigest="abc"
        )
        hexdigest_file_2 = SnapshotFile(
            relative_path=Path("file2"), file_size=5, stored_file_size=1, mtime_ns=0, hexdigest="def"
        )
        files = [hexdigest_file_1, hexdigest_file_2]
        snapshot_result = SnapshotResult(state=SnapshotState(root_globs=["**/*"], files=files, empty_dirs=[]))
        delta_chunks, hexdigests = DeltaBaseBackup._split_files_for_upload(  # pylint: disable=protected-access
            snapshot_result=snapshot_result, snapshot_dir=Path("/dir"), chunk_size=1, skip_hexdigests={"abc"}
        )
        assert hexdigests == {"def"}
        assert not delta_chunks

    def test_submit_files_in_thread_fails_when_file_disappears(self, deltabasebackup, delta_paths, delta_file, snapshotter):
        _, dst = delta_paths
        file_name, file_hash = delta_file

        with patch.object(deltabasebackup, "_delta_upload_hexdigest"):
            with snapshotter.lock:
                deltabasebackup._snapshot(snapshotter=snapshotter)  # pylint: disable=protected-access
                (dst / file_name).unlink()
                assert not deltabasebackup._submit_files_in_thread(  # pylint: disable=protected-access
                    snapshotter=snapshotter, callback_queue=CallbackQueue(), new_hashes={}, hexdigest=file_hash
                )

    def test_submit_files_in_thread_skip_upload(self, deltabasebackup, snapshotter, delta_file):
        _, file_hash = delta_file

        with patch.object(deltabasebackup, "_delta_upload_hexdigest") as mock_delta_upload_hexdigest:
            mock_delta_upload_hexdigest.return_value = (200, 10, file_hash, True)
            new_hashes = {}
            with snapshotter.lock:
                deltabasebackup._snapshot(snapshotter=snapshotter)  # pylint: disable=protected-access
                assert deltabasebackup._submit_files_in_thread(  # pylint: disable=protected-access
                    snapshotter=snapshotter, callback_queue=CallbackQueue(), new_hashes=new_hashes, hexdigest=file_hash
                )
                assert file_hash in new_hashes

    def test_submit_files_in_thread_exception_silenced(self, deltabasebackup, snapshotter, delta_file):
        _, file_hash = delta_file

        with patch.object(deltabasebackup, "_delta_upload_hexdigest") as mock_delta_upload_hexdigest:
            mock_delta_upload_hexdigest.side_effect = Exception
            with snapshotter.lock:
                deltabasebackup._snapshot(snapshotter=snapshotter)  # pylint: disable=protected-access
                assert not deltabasebackup._submit_files_in_thread(  # pylint: disable=protected-access
                    snapshotter=snapshotter, callback_queue=CallbackQueue(), new_hashes={}, hexdigest=file_hash
                )

    def test_submit_files_in_thread_normal_upload(self, deltabasebackup, snapshotter, delta_file):
        _, file_hash = delta_file

        callback_queue = CallbackQueue()
        new_hashes = {}

        callback_queue.get = Mock()
        callback_queue.get.side_effect = [Empty, None]
        with patch.object(deltabasebackup, "_delta_upload_hexdigest") as mock_delta_upload_hexdigest:
            mock_delta_upload_hexdigest.side_effect = callback_queue.put("abc")
            mock_delta_upload_hexdigest.return_value = (200, 10, file_hash, False)

            with snapshotter.lock:
                deltabasebackup._snapshot(snapshotter=snapshotter)  # pylint: disable=protected-access
                assert deltabasebackup._submit_files_in_thread(  # pylint: disable=protected-access
                    snapshotter=snapshotter, callback_queue=callback_queue, new_hashes=new_hashes, hexdigest=file_hash,
                    queue_timeout=0.1
                )

            assert file_hash in new_hashes

    @pytest.mark.parametrize("skip_upload", [True, False])
    def test_delta_upload_hexdigest(self, skip_upload, deltabasebackup, tmp_path):
        file_path = tmp_path / "new_file.dat"
        with open(file_path, "bw") as f:
            f.write(b"some data" * 100)

        chunk_path = tmp_path / "chunks"
        chunk_path.mkdir()

        if skip_upload:
            deltabasebackup.submitted_hashes = {"5cc8204133a65382a9045c82ffe166fb561e9d3f7c34babf85ed960e6195ea09"}

        with open(file_path, "rb") as f:
            assert deltabasebackup._delta_upload_hexdigest(  # pylint: disable=protected-access
                temp_dir=tmp_path,
                chunk_path=chunk_path / "some file",
                file_obj=f,
                callback_queue=CallbackQueue(),
                relative_path="does not matter"
            ) == (900, 240, "5cc8204133a65382a9045c82ffe166fb561e9d3f7c34babf85ed960e6195ea09", skip_upload)

    @pytest.mark.parametrize("key_exists", [True, False])
    def test_upload_single_delta_files_cleanup_after_error(self, deltabasebackup, snapshotter, delta_file, key_exists):
        _, file_hash = delta_file

        with patch.object(deltabasebackup, "_delta_upload_hexdigest") as mock_delta_upload_hexdigest:
            mock_delta_upload_hexdigest.return_value = (200, 10, file_hash, True)
            snapshotter.update_snapshot_file_data = Mock(side_effect=Exception)

            if not key_exists:
                deltabasebackup.storage.delete_key.side_effect = FileNotFoundFromStorageError

            with snapshotter.lock:
                deltabasebackup._snapshot(snapshotter=snapshotter)  # pylint: disable=protected-access
                with pytest.raises(BackupFailure):
                    deltabasebackup._upload_single_delta_files(todo_hexdigests={file_hash}, snapshotter=snapshotter)  # pylint: disable=protected-access
                deltabasebackup.storage.delete_key.assert_called_with(
                    "abc/basebackup_delta/2b033f9f5ba9cf20671da79e492f41545e673b562603945ffed09662fd92321a"
                )

    def test_upload_single_delta_files(self, deltabasebackup, snapshotter, delta_file):
        _, file_hash = delta_file

        with patch.object(deltabasebackup, "_delta_upload_hexdigest") as mock_delta_upload_hexdigest:
            mock_delta_upload_hexdigest.return_value = (200, 10, file_hash, True)
            with snapshotter.lock:
                deltabasebackup._snapshot(snapshotter=snapshotter)  # pylint: disable=protected-access
                metric = deltabasebackup._upload_single_delta_files(todo_hexdigests={file_hash}, snapshotter=snapshotter)  # pylint: disable=protected-access
                assert metric == UploadedFilesMetric(input_size=200, stored_size=10, count=1)

    def test_read_delta_sizes(self, deltabasebackup):
        files = [
            SnapshotFile(relative_path=Path("f1"), file_size=100, stored_file_size=20, mtime_ns=0, should_be_bundled=True),
            SnapshotFile(
                relative_path=Path("f2"),
                file_size=100,
                stored_file_size=20,
                mtime_ns=0,
                should_be_bundled=False,
                hexdigest="a"
            ),
            SnapshotFile(
                relative_path=Path("f3"),
                file_size=200,
                stored_file_size=40,
                mtime_ns=0,
                should_be_bundled=False,
                hexdigest="b"
            ),
            SnapshotFile(
                relative_path=Path("f4"),
                file_size=5,
                stored_file_size=5,
                mtime_ns=0,
                should_be_bundled=False,
                hexdigest="",
                content_b64="b64"
            )
        ]
        snapshot_result = SnapshotResult(state=SnapshotState(root_globs=["**/*"], files=files, empty_dirs=[]))
        digest_metric, embed_metric = deltabasebackup._read_delta_sizes(snapshot_result=snapshot_result)  # pylint: disable=protected-access
        assert digest_metric == UploadedFilesMetric(input_size=300, stored_size=60, count=2)
        assert embed_metric == UploadedFilesMetric(input_size=5, stored_size=5, count=1)

        # Add one more file which should not be uploaded as it was already uploaded previously, so the stored_file_size
        # should be restored from already tracked files
        files.append(
            SnapshotFile(
                relative_path=Path("f5"),
                file_size=300,
                stored_file_size=0,
                mtime_ns=0,
                should_be_bundled=False,
                hexdigest="c"
            )
        )
        snapshot_result = SnapshotResult(state=SnapshotState(root_globs=["**/*"], files=files, empty_dirs=[]))
        deltabasebackup.tracked_snapshot_files = {
            "c": SnapshotFile(
                relative_path=Path("f5"),
                file_size=300,
                stored_file_size=60,
                mtime_ns=0,
                should_be_bundled=False,
                hexdigest="c"
            )
        }
        digest_metric, embed_metric = deltabasebackup._read_delta_sizes(snapshot_result=snapshot_result)  # pylint: disable=protected-access
        assert digest_metric == UploadedFilesMetric(input_size=600, stored_size=120, count=3)
        assert embed_metric == UploadedFilesMetric(input_size=5, stored_size=5, count=1)
