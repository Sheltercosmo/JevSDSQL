"""One structured generation, with an explicit transport and no repair retries."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import httpx

from ..evaluators import ProviderError


class StructuredLLM:
    def __init__(self, model=None, *, transport=None, effort="low", timeout=240):
        self.transport = transport or os.getenv("SDD_LLM_TRANSPORT", "openai")
        self.model = (
            model
            or os.getenv("SDD_LLM_MODEL")
            or ("gpt-5.6-terra" if self.transport == "codex_cli" else "")
        )
        self.effort, self.timeout = effort, timeout

    def generate(self, prompt, schema):
        started = time.perf_counter()
        try:
            if self.transport == "openai":
                value, usage = self._responses(prompt, schema)
            elif self.transport == "codex_cli":
                value, usage = self._cli(prompt, schema)
            else:
                raise ProviderError("UnknownLLMTransport", False)
        except (httpx.RequestError, subprocess.TimeoutExpired) as exc:
            raise ProviderError("LLMTimeoutOrConnectionFailure", True) from exc
        except (ValueError, KeyError, OSError) as exc:
            raise ProviderError("InvalidLLMResponse", False) from exc
        return value, {
            "model": self.model,
            "transport": self.transport,
            "calls": 1,
            "generation_ms": round((time.perf_counter() - started) * 1000, 2),
            "usage": usage,
        }

    def _responses(self, prompt, schema):
        if not self.model:
            raise ProviderError("LLMModelNotConfigured", False)
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise ProviderError("OpenAIKeyNotConfigured", False)
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": "Bearer " + key},
            json={
                "model": self.model,
                "input": prompt,
                "store": False,
                "reasoning": {"effort": self.effort},
                "max_output_tokens": 1200 if schema.get("title") == "Concepts" else 8000,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "sql_plan",
                        "schema": schema,
                        "strict": True,
                    }
                },
            },
            timeout=self.timeout,
        )
        if not response.is_success:
            raise ProviderError(
                "LLM_HTTP_" + str(response.status_code), response.status_code >= 500
            )
        result = response.json()
        if result.get("status") != "completed":
            raise ProviderError("LLMOutputIncomplete", False)
        output = "".join(
            part["text"]
            for item in result["output"]
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        )
        return json.loads(output), result.get("usage", {})

    def _cli(self, prompt, schema):
        executable = os.getenv("SDD_CODEX_EXECUTABLE") or shutil.which("codex")
        if not executable:
            raise ProviderError("CodexCLINotConfigured", False)
        with tempfile.TemporaryDirectory(prefix="sdd-llm-") as directory:
            work = Path(directory)
            schema_path, answer_path = work / "schema.json", work / "answer.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            command = [
                executable,
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                self.model,
                "--json",
                "--color",
                "never",
                "--cd",
                directory,
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(answer_path),
                "-c",
                'model_reasoning_effort="' + self.effort + '"',
                "-c",
                'web_search="disabled"',
                "-c",
                "project_doc_max_bytes=0",
            ]
            for feature in (
                "shell_tool",
                "unified_exec",
                "apps",
                "plugins",
                "multi_agent",
                "browser_use",
                "computer_use",
                "image_generation",
                "view_image",
                "memories",
            ):
                command.extend(["--disable", feature])
            command.extend(
                [
                    "-c",
                    'model_provider="sdd_http"',
                    "-c",
                    'model_providers.sdd_http.name="OpenAI Codex HTTP"',
                    "-c",
                    "model_providers.sdd_http.requires_openai_auth=true",
                    "-c",
                    "model_providers.sdd_http.supports_websockets=false",
                    "-c",
                    'model_providers.sdd_http.wire_api="responses"',
                    "-",
                ]
            )
            process = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            events = []
            for line in process.stdout.splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
            if any(
                event.get("item", {}).get("type")
                in {
                    "command_execution",
                    "mcp_tool_call",
                    "web_search",
                    "file_change",
                }
                for event in events
            ):
                raise ProviderError("LLMUnexpectedToolUse", False)
            if process.returncode or not answer_path.exists():
                raise ProviderError("LLMGenerationFailed", False)
            usage = next(
                (e.get("usage", {}) for e in events if e.get("type") == "turn.completed"), {}
            )
            return json.loads(answer_path.read_text(encoding="utf-8")), usage
