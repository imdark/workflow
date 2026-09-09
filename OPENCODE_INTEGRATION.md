# OpenCode AI Provider Integration

This workflow now supports OpenCode as an AI provider for development assistance.

## Usage

### Set OpenCode as your AI provider:
```bash
wf ai-provider opencode
```

### Set Claude as your AI provider:
```bash
wf ai-provider claude
```

### Launch AI session with configured provider:
```bash
wf ai
```

The command will show which provider is being used:
```
🤖 Using AI provider: opencode
```

## Configuration

The AI provider is stored in your workflow configuration file (`~/.wf/config.yaml`):

```yaml
ai:
  provider: opencode  # or claude
```

You can also set it using the generic config command:
```bash
wf set-config ai.provider opencode
```

## Implementation Details

- OpenCode provider is implemented in `workflow/ai_providers/opencode.py`
- It follows the same `AIProvider` interface as the existing Claude provider
- The provider pipes context to the `opencode` command-line tool
- All existing RAG and memory features work with both providers

## Requirements

- OpenCode CLI tool must be installed and available in your PATH
- Use `wf ai-provider opencode` to configure it as your provider