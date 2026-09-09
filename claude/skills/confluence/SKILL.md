---
name: confluence
description: Edit Confluence pages using wf browser CLI. Use when the user wants to edit, update, or create content in Confluence.
disable-model-invocation: true
---

# Confluence Page Editing Skill

Use the `wf browser` CLI commands to interact with Confluence pages.

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

### List Open Browser Pages
```bash
wf browser pages
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
# or from file:
wf browser eval --file /path/to/script.js
```

## Sidebar Page Listing

### List All Pages in Sidebar
To get all documentation pages from the Confluence sidebar navigation:

```bash
wf browser eval --file /tmp/list_sidebar.js
```

Create `/tmp/list_sidebar.js`:
```javascript
var links = Array.from(document.querySelectorAll('nav a[href*="/pages/"]')).map(function(a) {
  return {
    title: a.textContent.trim(),
    url: a.href
  };
}).filter(function(l) { return l.title; });
console.log(JSON.stringify(links, null, 2));
links;
```

### Quick One-Liner
```bash
wf browser eval "JSON.stringify(Array.from(document.querySelectorAll('nav a[href*=\"/pages/\"]')).map(function(a){return{title:a.textContent.trim(),url:a.href}}).filter(function(l){return l.title}))"
```

## Iterative Sidebar Navigation

**IMPORTANT**: Navigate through the sidebar to explore, only use `wf browser open` for the final page you want to edit.

Confluence sidebar is hierarchical - you need to expand parent pages to see their children.

### Workflow: Navigate to Target Page

1. Open the space (this loads the sidebar):
   ```bash
   wf browser open "https://your-domain.atlassian.net/wiki/spaces/SPACE"
   ```

2. Expand all sidebar items:
   ```bash
   wf browser eval "document.querySelectorAll('button[aria-expanded]').forEach(function(b){b.click()})"
   ```

3. List available pages:
   ```bash
   wf browser eval --file /tmp/list_sidebar.js
   ```

4. Click to navigate in sidebar (explore intermediate pages):
   ```bash
   # Click "Child Page" in sidebar (doesn't reload page, just navigates)
   wf browser eval "var l=Array.from(document.querySelectorAll('a')).find(function(a){return a.textContent.includes('Child Page')});if(l){l.click();'clicked'}else{'not found'}"
   ```

5. List pages at new level:
   ```bash
   wf browser eval --file /tmp/list_sidebar.js
   ```

6. Continue clicking through sidebar until you reach the target

7. Only NOW open the final page for editing:
   ```bash
   wf browser open "https://your-domain.atlassian.net/wiki/spaces/SPACE/pages/PAGE_ID/Page+Title"
   ```

### Why This Matters

- Clicking sidebar links keeps you in the same browser context
- You can explore the hierarchy step by step
- Only use `wf browser open` when you need to load a specific page for editing
- This is faster and more reliable than trying to construct URLs
   ```

## Common Confluence Selectors

- Sidebar nav: `nav`, `[class*="sidebar"]`, `#sidebar`
- Editor: `.ProseMirror`
- First paragraph: `.ProseMirror p`
- Save button: `button[data-testid="publish-button"]`
- Title input: `input[data-testid="title-textfield"]`
- Quick search: `#quick-search-input`
- Page title heading: `[data-testid="title-heading"]`

## Workflow: Find and Navigate to a Page

1. Open Confluence space:
   ```bash
   wf browser open "https://your-company.atlassian.net/wiki/spaces/SPACE"
   ```

2. Get sidebar pages:
   ```bash
   wf browser eval --file /tmp/list_sidebar.js
   ```

3. Find page URL from the output, then open it:
   ```bash
   wf browser open "https://your-company.atlassian.net/wiki/spaces/SPACE/pages/1234567/Page+Name"
   ```

4. Edit the content:
   ```bash
   wf browser eval "document.querySelector('.ProseMirror').innerHTML = '<p>Your content</p>'"
   ```

5. Take screenshot to verify:
   ```bash
   wf browser screenshot
   ```

## Using Confluence Search

The most efficient way to find a page is using Confluence's search.

### Search via URL (Recommended)

This is the most reliable method:

```bash
wf browser open "https://your-domain.atlassian.net/wiki/search?text=Page+Title"
```

Then click the result:

```bash
wf browser eval "var l=Array.from(document.querySelectorAll('a')).find(function(a){return a.textContent.includes('Example Page Title')});if(l){l.click();'clicked'}else{'not found'}"
```

### Quick Search (Press `/`)

1. Press `/` to open quick search
2. Type your search query
3. Use arrow keys to navigate results
4. Press Enter to open

```bash
# Focus global search and type
wf browser eval "document.querySelector('input[placeholder*=\"Search Confluence\"]').focus()"
wf browser eval "document.querySelector('input[placeholder*=\"Search Confluence\"]').value = 'Example Page Title'"
```

### Search and Navigate Script

```javascript
// Search for a page and click first result
var searchTerm = 'Example Search Term';
var searchInput = document.querySelector('input[placeholder*="Search Confluence"]');
if (searchInput) {
  searchInput.focus();
  searchInput.value = searchTerm;
  searchInput.dispatchEvent(new Event('input', {bubbles: true}));
  
  // Wait for results and click first link
  setTimeout(function() {
    var firstResult = document.querySelector('a[href*="/pages/"]');
    if (firstResult) {
      firstResult.click();
      'Opened: ' + searchTerm;
    } else {
      'No results found';
    }
  }, 1500);
} else {
  'Search input not found';
}
```

## Notes

- Use single quotes inside JavaScript strings to avoid escaping issues
- For multi-line scripts, use `wf browser eval --file /path/to/script.js`
- The daemon must be running for commands to work
- Always open a page first before running eval: `wf browser open <url>`
