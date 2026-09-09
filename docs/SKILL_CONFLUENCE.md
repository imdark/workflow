# Confluence Page Editing Skill

This skill enables LLM agents to edit Confluence pages using the `wf browser` CLI commands.

## Prerequisites

1. Chrome must be running with remote debugging:
   ```bash
   /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --remote-allow-origins=*
   ```

2. Start the browser daemon:
   ```bash
   wf browser connect
   ```

## Commands

### Navigate to Confluence
```bash
wf browser open <confluence-url>
```

### Take Screenshot
```bash
wf browser screenshot
```

### Fill Text (plain text)
```bash
wf browser fill "<selector>" "<text>"
```

### Set Rich Text (HTML)
```bash
wf browser eval "document.querySelector('<selector>').innerHTML = '<b>bold</b> and <i>italic</i>'"
```

### Click Elements
```bash
wf browser click "<selector>"
```

### Press Keys
```bash
wf browser press "Enter"
wf browser press "Meta+v"  # Paste
```

### Wait for Element
```bash
wf browser wait "<selector>"
```

### Evaluate JavaScript
```bash
wf browser eval "<js-code>"
```

## Common Confluence Selectors

- Editor: `.ProseMirror`
- First paragraph: `.ProseMirror p`
- Save button: `button[data-testid="publish-button"]` or `button:contains("Publish")`
- Title input: `input[data-testid="title-textfield"]`

## Workflow for Editing a Confluence Page

1. Open the page:
   ```bash
   wf browser open "https://your-company.atlassian.net/wiki/spaces/SPACE/pages/1234567/Page+Name"
   ```

2. Wait for editor to load:
   ```bash
   wf browser wait ".ProseMirror"
   ```

3. Fill the content:
   ```bash
   wf browser eval "document.querySelector('.ProseMirror').innerHTML = '<p>Your content here</p>'"
   ```

4. Or use fill for plain text:
   ```bash
   wf browser fill ".ProseMirror" "Plain text content"
   ```

5. Take screenshot to verify:
   ```bash
   wf browser screenshot
   ```

## Notes

- Use single quotes inside JavaScript strings to avoid escaping issues
- For multi-line scripts, write to a temp file and use eval with file contents
- The daemon must be running for commands to work
