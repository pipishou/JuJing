"""LLM 通道单测（离线：只测配置解析与错误路径，不联网）。

真联网验通由 `python -B core/llm.py --check` 完成（人工/CI 有 key 时跑）。
"""
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import llm  # noqa: E402

CLEAN_ENV = ("BOT_LLM_PROVIDER", "BOT_LLM_MODEL", "BOT_LLM_KEY", "BOT_LLM_BASE",
             "BOT_LLM_ENV_FILE", "DEEPSEEK_API_KEY", "BIGMODEL_API_KEY",
             "DSH_OPENCODE_GO_KEY", "BOT_LLM_MODEL_DEEPSEEK", "BOT_LLM_SESSION")


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in CLEAN_ENV}
        self.tmp = tempfile.TemporaryDirectory()
        self.env_file = pathlib.Path(self.tmp.name) / ".env.local"
        os.environ["BOT_LLM_ENV_FILE"] = str(self.env_file)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def write_env(self, text):
        self.env_file.write_text(text, encoding="utf-8")


class TestEnvFile(Base):
    def test_missing_file_is_empty(self):
        self.assertEqual(llm.load_env_file(), {})

    def test_parses_kv_ignores_comments_and_quotes(self):
        self.write_env('# 注释\n\nA=1\nB = "two"\nC=\'three\'\nBADLINE\n')
        data = llm.load_env_file()
        self.assertEqual(data, {"A": "1", "B": "two", "C": "three"})


class TestConfig(Base):
    def test_no_key_raises_fail_closed(self):
        self.write_env("BOT_LLM_PROVIDER=opencode\n")
        with self.assertRaises(llm.LLMError) as ctx:
            llm.config()
        self.assertIn("没有拿到 opencode 的 key", str(ctx.exception))

    def test_default_provider_opencode_with_headers(self):
        self.write_env("BOT_LLM_KEY=oc_sk_test\n")
        cfg = llm.config()
        self.assertEqual(cfg["provider"], "opencode")
        self.assertEqual(cfg["base"], "https://opencode.ai/inference/go/v1")
        self.assertEqual(cfg["model"], "deepseek-v4.1-flash")
        self.assertIn("x-opencode-session", cfg["headers"])

    def test_unknown_provider_raises(self):
        self.write_env("BOT_LLM_PROVIDER=nope\nBOT_LLM_KEY=x\n")
        with self.assertRaises(llm.LLMError) as ctx:
            llm.config()
        self.assertIn("未知 provider", str(ctx.exception))

    def test_model_is_provider_scoped(self):
        """BOT_LLM_MODEL 只作用于配置的 provider，不该污染别的 provider。"""
        self.write_env("BOT_LLM_PROVIDER=opencode\nBOT_LLM_MODEL=deepseek-v4.1-flash\n"
                       "BOT_LLM_KEY=oc_sk_test\nDEEPSEEK_API_KEY=sk_test\n")
        self.assertEqual(llm.config()["model"], "deepseek-v4.1-flash")
        self.assertEqual(llm.config("deepseek")["model"], "deepseek-chat")
        self.assertEqual(llm.config("bigmodel")["model"], "glm-5.3-flash")

    def test_provider_specific_model_override_wins(self):
        self.write_env("BOT_LLM_PROVIDER=opencode\nBOT_LLM_KEY=oc_sk_test\n"
                       "BOT_LLM_MODEL_DEEPSEEK=deepseek-reasoner\nDEEPSEEK_API_KEY=sk_test\n")
        self.assertEqual(llm.config("deepseek")["model"], "deepseek-reasoner")

    def test_key_precedence_per_provider(self):
        self.write_env("BOT_LLM_PROVIDER=opencode\nBOT_LLM_KEY=oc_sk_test\n"
                       "DEEPSEEK_API_KEY=sk_test\nBIGMODEL_API_KEY=bm_test\n")
        self.assertEqual(llm.config()["key"], "oc_sk_test")
        self.assertEqual(llm.config("deepseek")["key"], "sk_test")
        self.assertEqual(llm.config("bigmodel")["key"], "bm_test")

    def test_opencode_falls_back_to_dsh_env_name(self):
        self.write_env("DSH_OPENCODE_GO_KEY=oc_sk_dsh\n")
        cfg = llm.config("opencode")
        self.assertEqual(cfg["key"], "oc_sk_dsh")
        self.assertEqual(cfg["key_source"], "DSH_OPENCODE_GO_KEY")

    def test_base_override(self):
        self.write_env("BOT_LLM_KEY=oc_sk_test\nBOT_LLM_BASE=https://example.test/v1/\n")
        self.assertEqual(llm.config()["base"], "https://example.test/v1")

    def test_real_env_ignored(self):
        os.environ["BOT_LLM_KEY"] = "from-os"
        self.write_env("")
        self.assertEqual(llm.config()["key"], "from-os")


class TestCli(Base):
    def test_cli_without_key_returns_2(self):
        self.write_env("")
        self.assertEqual(llm.main(["--check"]), 2)

    def test_cli_models_without_key_returns_2(self):
        self.write_env("")
        self.assertEqual(llm.main(["--models"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
