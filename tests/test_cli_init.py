"""Tests for the lobster init CLI command."""

from unittest.mock import AsyncMock, MagicMock, patch

from remote_control.cli_init import _validate_credentials, init_config


class TestValidateCredentials:
    """Tests for WeCom credential validation."""

    async def test_valid_credentials(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"errcode": 0, "access_token": "tok"}

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_client
        mock_cm.__aexit__.return_value = False

        with patch("remote_control.cli_init.httpx.AsyncClient", return_value=mock_cm):
            ok, msg = await _validate_credentials("corp123", "secret456")

        assert ok is True
        assert "successfully" in msg

    async def test_invalid_credentials(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"errcode": 40013, "errmsg": "invalid corpid"}

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_client
        mock_cm.__aexit__.return_value = False

        with patch("remote_control.cli_init.httpx.AsyncClient", return_value=mock_cm):
            ok, msg = await _validate_credentials("bad", "bad")

        assert ok is False
        assert "40013" in msg


class TestInitConfig:
    """Tests for the interactive config generator."""

    def test_generates_config_file(self, tmp_path, monkeypatch):
        """Test that init_config writes a valid config.yaml."""
        monkeypatch.chdir(tmp_path)

        # Simulate user inputs in order:
        # corp_id, agent_id, secret, token, aes_key, name, mode, relay_url, working_dir
        inputs = iter([
            "corp123",       # Corp ID
            "1000002",       # Agent ID
            "secret456",     # Secret
            "mytoken",       # Token
            "a" * 43,        # Encoding AES Key
            "test-bot",      # Name
            "relay",         # Mode
            "https://relay.example.com",  # Relay URL
            str(tmp_path),   # Working dir
        ])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        # Mock the validation to succeed
        with patch("remote_control.cli_init.asyncio.run", return_value=(True, "OK")):
            init_config()

        config_path = tmp_path / "config.yaml"
        assert config_path.exists()
        content = config_path.read_text()
        assert "corp123" in content
        assert "1000002" in content
        assert "secret456" in content
        assert "mytoken" in content
        assert "test-bot" in content
        assert "https://relay.example.com" in content

    def test_aborts_on_existing_config_no_overwrite(self, tmp_path, monkeypatch):
        """Test that existing config.yaml is not overwritten when user says no."""
        monkeypatch.chdir(tmp_path)
        config_path = tmp_path / "config.yaml"
        config_path.write_text("original content")

        # Answer 'N' to overwrite prompt
        monkeypatch.setattr("builtins.input", lambda _: "N")

        init_config()

        assert config_path.read_text() == "original content"

    def test_overwrites_existing_config_on_yes(self, tmp_path, monkeypatch):
        """Test that existing config.yaml is overwritten when user confirms."""
        monkeypatch.chdir(tmp_path)
        config_path = tmp_path / "config.yaml"
        config_path.write_text("original content")

        # First input: 'y' for overwrite, then the rest
        inputs = iter([
            "y",             # Overwrite
            "corp123",       # Corp ID
            "1000002",       # Agent ID
            "secret456",     # Secret
            "mytoken",       # Token
            "a" * 43,        # AES Key
            "test-bot",      # Name
            "relay",         # Mode
            "https://relay.example.com",  # Relay URL
            str(tmp_path),   # Working dir
        ])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        with patch("remote_control.cli_init.asyncio.run", return_value=(True, "OK")):
            init_config()

        assert config_path.read_text() != "original content"
        assert "corp123" in config_path.read_text()
