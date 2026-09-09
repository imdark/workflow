# Smart Custom Fields with `wf start`

The `wf start` command now includes **smart custom field discovery** that automatically resolves Jira field names to their internal IDs, making configuration much easier.

## New Features

### 🔍 **Auto Field Resolution**
No more hunting for field IDs! The system now accepts field names and automatically resolves them:
```bash
# Before (required manual field ID lookup)
wf config add-field POD customfield_10100 --project-key=DATA

# After (uses field name, auto-resolves to ID)
wf config add-field POD POD --project-key=DATA
```

### 🔍 **Field Discovery Commands**
```bash
# Search for specific fields
wf config search-fields "priority"
wf config search-fields "POD"

# Show all available fields
wf config search-fields

# Refresh field discovery cache
wf config discover-fields
```

## Enhanced Usage

### 1. Smart Field Addition
```bash
# Add using field name (preferred)
wf config add-field SPRINT "Sprint"
wf config add-field STORY_POINTS "Story Points"

# Add with project restriction
wf config add-field TEAM "Team" --project-key=DEV

# Add using field ID (still supported)
wf config add-field EPIC_EPIC epic --project-key=DATA
```

### 2. Field Search and Discovery
```bash
# Find fields by name
wf config search-fields "story"
wf config search-fields "epic"

# Show all fields with types
wf config search-fields
```

**Sample Output:**
```
Found 3 fields:
  Story Points: customfield_10004 [number] (custom)
  Epic Name: customfield_10011 [string] (custom)
  Epic Link: customfield_10014 [string] (custom)
```

### 3. Smart Field Application
```bash
# Works with auto-resolved fields
wf start DATA-123 --POD=backend --SPRINT="Sprint 42"
wf start DEV-456 --STORY_POINTS=8 --TEAM=infrastructure
```

## Field Type Support

The system now intelligently handles different Jira field types:

| Field Type | Example Input | How it's processed |
|------------|---------------|------------------|
| **String** | `--field=text` | Direct string value |
| **Number** | `--field=8.5` | Converted to number |
| **Array** | `--field="tag1, tag2"` | Split into array |
| **User** | `--field="user@email.com"` | Converted to user object |
| **Option** | `--field=Backend` | Single-select option |
| **Date/Datetime** | `--field=2024-01-15` | Date format preserved |

## Configuration Storage

Fields are stored with resolved IDs for reliability:

```yaml
custom_fields:
  POD:
    field_id: customfield_10373      # Auto-resolved ID
    project_key: DATA
  STORY_POINTS:
    field_id: customfield_10004      # Auto-resolved ID
    project_key: ""
```

## Advanced Features

### **Partial Name Matching**
```bash
# Finds "Story Points" with partial match
wf config add-field POINTS "story"
# ✅ Resolved 'story' → Story Points (customfield_10004)
```

### **Error Recovery**
If exact name isn't found, system suggests alternatives:
```bash
wf config add-field PRIORITY "urgent"
# ⚠️  No field found matching 'urgent'
# 💡 Found similar fields:
#   - Priority: priority
```

### **Caching Performance**
- Field discovery cached for 1 hour
- Automatic cache refresh on discovery
- Fast subsequent lookups

## Migration Guide

### From Manual IDs to Smart Names
1. **Check current configuration:**
   ```bash
   wf config list-fields
   ```

2. **Add new fields using names:**
   ```bash
   # Old field with manual ID
   # TEAM: customfield_10101
   
   # Add using smart name resolution
   wf config add-field TEAM_NEW "Team"
   ```

3. **Remove old manual entries:**
   ```bash
   wf config remove-field TEAM
   ```

4. **Update usage:**
   ```bash
   # Old: --TEAM_NEW=value
   wf start TASK-123 --TEAM_NEW=backend
   ```

## Troubleshooting

### **Field Not Found**
```bash
wf config add-field UNKNOWN "mystery"
# ❌ No field found matching 'mystery'

# Solution: Search for similar fields
wf config search-fields "mystery"
```

### **Wrong Field Type**
```bash
# If value gets rejected, check field type
wf config search-fields "priority"
# Priority: priority [priority]

# Use appropriate value format
wf start TASK-123 --PRIORITY="High"  # Option field uses name
```

### **Stale Cache**
```bash
# If recently added fields aren't found
wf config discover-fields
```

## Benefits

✅ **No more manual field ID lookup**  
✅ **Type-aware field processing**  
✅ **Partial name matching**  
✅ **Helpful error messages**  
✅ **Fast caching**  
✅ **Backward compatibility**  

The smart field system makes custom field configuration dramatically easier while maintaining full compatibility with existing workflows.