import pytest
import os
import tempfile
import yaml
from pathlib import Path
from unittest.mock import patch


class TestAIStateSessionCaseSensitivity:
    """Tests for AI session state management with case sensitivity."""

    @pytest.fixture
    def temp_state_dir(self, tmp_path):
        """Create a temporary state directory."""
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir

    @pytest.fixture
    def mock_state_path(self, temp_state_dir, monkeypatch):
        """Mock the state file path to use temp directory."""
        state_file = temp_state_dir / "state.yaml"
        monkeypatch.setattr("workflow.state.STATE", state_file)
        return state_file

    def test_set_ai_session_normalizes_key_to_uppercase(self, mock_state_path):
        """Test that set_ai_session stores keys in uppercase for consistency."""
        from workflow.state import set_ai_session, clear_ai_session

        task_key = "task-53"
        set_ai_session(task_key, 12345)

        state = yaml.safe_load(mock_state_path.read_text())
        assert "ai_sessions" in state
        assert "TASK-53" in state["ai_sessions"]
        assert "task-53" not in state["ai_sessions"]

        clear_ai_session("TASK-53")

    def test_get_all_ai_sessions_cleanup_stale_entries(self, temp_state_dir, monkeypatch):
        """Test that get_all_ai_sessions properly cleans up stale PID entries."""
        from workflow.state import get_all_ai_sessions, set_ai_session, clear_ai_session

        task_key = "task-53"
        task_dir = temp_state_dir / "tasks" / task_key.lower()
        task_dir.mkdir(parents=True, exist_ok=True)
        pid_file = task_dir / "ai.pid"
        pid_file.write_text(f"{99999}\n2024-01-01T00:00:00\n")

        monkeypatch.setattr("workflow.state.STATE", temp_state_dir / "state.yaml")

        set_ai_session(task_key, 99999)

        sessions = get_all_ai_sessions()
        assert task_key.upper() not in sessions

        state = yaml.safe_load((temp_state_dir / "state.yaml").read_text())
        assert "ai_sessions" not in state or task_key.upper() not in state.get("ai_sessions", {})

    def test_has_ai_session_returns_false_for_stale_process(self, temp_state_dir, monkeypatch):
        """Test that has_ai_session returns False for a dead process."""
        from workflow.state import has_ai_session, set_ai_session

        task_key = "task-53"
        task_dir = temp_state_dir / "tasks" / task_key.lower()
        task_dir.mkdir(parents=True, exist_ok=True)
        pid_file = task_dir / "ai.pid"
        pid_file.write_text(f"{99999}\n2024-01-01T00:00:00\n")

        monkeypatch.setattr("workflow.state.STATE", temp_state_dir / "state.yaml")

        set_ai_session(task_key, 99999)

        result = has_ai_session(task_key)
        assert result is False

    def test_case_insensitive_task_key_matching(self, mock_state_path):
        """Test that state.yaml keys are normalized to uppercase for consistency."""
        from workflow.state import _load

        state = {
            "ai_sessions": {
                "task-53": {"pid": 12345, "started_at": "2024-01-01T00:00:00"}
            }
        }
        mock_state_path.write_text(yaml.dump(state))

        from workflow.state import clear_ai_session
        clear_ai_session("TASK-53")

        updated_state = yaml.safe_load(mock_state_path.read_text())
        assert "ai_sessions" not in updated_state or "task-53" not in updated_state.get("ai_sessions", {})
