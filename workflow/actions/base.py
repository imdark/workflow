from abc import ABC, abstractmethod
from typing import Dict, Any
from enum import Enum
import logging


class ActionType(Enum):
    """Enumeration of available action types"""
    NOTIFICATION = "notification"
    START_TASK = "start_task"
    FINISH_TASK = "finish_task"
    OPEN_AI_CONSOLE = "open_ai_console"
    OPEN_TERMINAL = "open_terminal"
    CUSTOM_COMMAND = "custom_command"
    ALIAS = "alias"
    BROWSER_OPEN = "browser_open"
    BROWSER_CLICK = "browser_click"
    BROWSER_TYPE = "browser_type"
    GITHUB_COMMENT_LOOKUP = "github_comment_lookup"


class Action(ABC):
    """Abstract base class for all actions"""
    
    def __init__(self, action_id: str, action_type: ActionType):
        self.action_id = action_id
        self.action_type = action_type
        self.logger = logging.getLogger(f"action.{action_type.value}")
    
    @abstractmethod
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute the action with given context
        
        Args:
            context: Dictionary containing relevant data for the action
            
        Returns:
            Dictionary containing execution result
        """
        pass
    
    @abstractmethod
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """
        Validate action configuration
        
        Args:
            config: Configuration dictionary for the action
            
        Returns:
            True if config is valid, False otherwise
        """
        pass
