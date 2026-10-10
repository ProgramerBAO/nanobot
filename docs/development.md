# Development

This page collects contributor-facing notes for extending nanobot. User-facing setup and runtime options live in [`configuration.md`](./configuration.md).

## Report intent routing verification (Phase 2)

Windows PowerShell, repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/tools/test_report_intent_router.py tests/tools/test_routing_eval.py tests/agent/test_magik_report_intent_route.py tests/webui/test_reporting_api.py -q
.\.venv\Scripts\python.exe -m ruff check nanobot/ tests/tools/test_report_intent_router.py scripts/evaluate_report_intents.py
.\.venv\Scripts\python.exe -m compileall -q nanobot scripts/evaluate_report_intents.py
```

The provider fixtures test validation/dispatch only, not model accuracy. The
regex-only corpus retains its historical paraphrase xfails independently; they
must not be flipped to passing based on mocked model responses. The realtime
token-throughput phrase is now a hard negative and RPM/current-week/current-month
requests that unsupported runners would reinterpret must clarify.

Explicit read-only live-model smoke (uses local Gateway configuration; sends
fixture phrases to the configured model, but never queries Cube, creates jobs or
sends Feishu messages):

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate_report_intents --today 2026-10-10 --limit 21
.\.venv\Scripts\python.exe -m scripts.evaluate_report_intents --today 2026-10-10 --limit 21 --timeout-seconds 120 --max-tokens 8192
```

Update `--today` to the client date when checking relative windows. A nonzero exit
means the corpus did not satisfy the selected classification gate. The second
command is the user-approved functional evaluation budget for a slower reasoning
model: elapsed time is recorded but not scored against the 3-second target. CLI
deadlines are finite (1–180 seconds) and token limits are bounded (128–8192).
These options do not modify Gateway or subscription timeouts. The first wide-budget
run matched 13/21 before slot fixes; a mock pass is not a replacement for live results.
Real Cube/Feishu integration acceptance is separate and has not been run.

WebUI (working directory `webui`):

```powershell
npm.cmd run test -- --run src/tests/reports-settings.test.tsx
npm.cmd run build
```

## Adding an LLM Provider

nanobot uses the provider registry in `nanobot/providers/registry.py` as the source of truth for LLM provider metadata. Most OpenAI-compatible providers need only two changes.

1. Add a `ProviderSpec` entry to `PROVIDERS`:

```python
ProviderSpec(
    name="myprovider",
    keywords=("myprovider", "mymodel"),
    env_key="MYPROVIDER_API_KEY",
    display_name="My Provider",
    default_api_base="https://api.myprovider.com/v1",
)
```

2. Add a field to `ProvidersConfig` in `nanobot/config/schema.py`:

```python
class ProvidersConfig(BaseModel):
    ...
    myprovider: ProviderConfig = Field(default_factory=ProviderConfig)
```

Environment variables, config matching, provider status, and WebUI credential display derive from those two entries.

Useful `ProviderSpec` options:

| Field | Description |
|---|---|
| `default_api_base` | Default OpenAI-compatible base URL. |
| `env_extras` | Additional environment variables derived from the provider config. |
| `model_overrides` | Per-model request parameter overrides. |
| `is_gateway` | Provider can route many model families, like OpenRouter. |
| `detect_by_key_prefix` | Match configured gateways by API-key prefix. |
| `detect_by_base_keyword` | Match configured gateways by API base URL. |
| `strip_model_prefix` | Strip `provider/` before sending the model to the upstream API. |
| `supports_max_completion_tokens` | Use `max_completion_tokens` instead of `max_tokens`. |
| `is_transcription_only` | Provider has credentials but cannot serve chat completions. |

## Adding a Transcription Provider

Transcription is intentionally split into two layers:

- `nanobot/audio/transcription_registry.py` owns provider names, aliases, default models, and adapter loading.
- `nanobot/providers/transcription.py` owns provider-specific HTTP behavior.

Credentials still live under `providers.<provider>` so chat channels and WebUI resolve API keys and API bases the same way.

1. Add provider credentials to `ProvidersConfig`.

```python
class ProvidersConfig(BaseModel):
    ...
    my_stt: ProviderConfig = Field(default_factory=ProviderConfig)
```

2. Add a `ProviderSpec` in `nanobot/providers/registry.py`.

For transcription-only providers, set `is_transcription_only=True` so they show up in credential/settings surfaces but stay out of chat model selection.

```python
ProviderSpec(
    name="my_stt",
    keywords=("my_stt",),
    env_key="MY_STT_API_KEY",
    display_name="My STT",
    default_api_base="https://api.example.com/v1",
    is_transcription_only=True,
)
```

3. Add an adapter class in `nanobot/providers/transcription.py`.

Adapters receive resolved credentials and settings. They return an empty string for provider errors so channel voice messages fail quietly instead of crashing the agent loop.

```python
class MySTTTranscriptionProvider:
    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        language: str | None = None,
        model: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("MY_STT_API_KEY")
        self.api_base = api_base or "https://api.example.com/v1"
        self.language = language or None
        self.model = model or "my-default-stt-model"

    async def transcribe(self, file_path: str | Path) -> str:
        ...
```

4. Register the adapter in `nanobot/audio/transcription_registry.py`.

```python
TranscriptionProviderSpec(
    name="my_stt",
    default_model="my-default-stt-model",
    adapter="nanobot.providers.transcription:MySTTTranscriptionProvider",
    aliases=("mystt",),
)
```

5. Add tests.

At minimum, cover:

- config resolution in `tests/providers/test_transcription.py`
- adapter request/response behavior and retry/error handling
- WebUI settings payload/update behavior in `tests/webui/test_settings_api.py`
- provider brand mapping if the provider appears in Settings

6. Update user-facing docs.

Add the provider to [`configuration.md`](./configuration.md) where users choose `transcription.provider`, but keep implementation details in this development guide.
