#!/usr/bin/env python3
"""Test script to verify the transition fix"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from workflow.config import load_config
from workflow.backends.jira import JiraBackend
from workflow.state import get_current_task

def test_transitions():
    try:
        cfg = load_config()
        backend = JiraBackend(cfg)
        issue = get_current_task()
        
        if not issue:
            print("❌ No current task found. Use 'wf start' to begin a task.")
            return False
            
        print(f"🔍 Current task: {issue.key}")
        
        # Get available transitions
        transitions = backend.client.transitions(issue.key)
        print(f"📋 Available transitions:")
        for t in transitions:
            print(f"  - {t['name']} (ID: {t['id']})")
        
        # Test the improved _transition method logic
        print("\n🧪 Testing transition mappings:")
        
        # Check if "Done" transition would work
        done_found = False
        for t in transitions:
            transition_name = t["name"].lower()
            if transition_name in ["done", "mark as done", "complete", "close", "closed", "resolve"]:
                print(f"✅ 'Done' transition found: {t['name']}")
                done_found = True
                break
        
        if not done_found:
            print("❌ No 'Done' transition found in available transitions")
            
        # Check if "Code Review" transition would work  
        review_found = False
        for t in transitions:
            transition_name = t["name"].lower()
            if transition_name in ["code review", "in review", "for review", "peer review", "review"]:
                print(f"✅ 'Code Review' transition found: {t['name']}")
                review_found = True
                break
                
        if not review_found:
            print("❌ No 'Code Review' transition found in available transitions")
            
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

if __name__ == "__main__":
    test_transitions()