import os
import sys
import json
import time
import re
import shutil
import hashlib
import logging
import importlib.util
import threading
import queue
import signal
import atexit
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any, Callable, Deque, Set, Union
from dataclasses import dataclass, field, asdict
from enum import Enum, auto
from collections import deque
from abc import ABC, abstractmethod
from functools import wraps, lru_cache
from logging.handlers import RotatingFileHandler
from contextlib import contextmanager
import traceback

try:
    import readline
    HAS_READLINE = True
except ImportError:
    HAS_READLINE = False

from config import MAX_TOOL_ITERATIONS, MAX_AUTO_TURNS
from models.registry import ModelRegistry, AVAILABLE_MODELS, ModelInfo
from tools import ToolRegistry
from .api_client import NVIDIAAPIClient, APIResponse
from .conversation import ConversationManager, Message
from .heavy_agent import HeavyAgent
from .chat_storage import ChatStorage, ChatMetadata
from .personality import get_personality_manager, PersonalityManager
from ui.colors import Colors
from ui.logo import print_logo, print_separator
from ui.rich_output import print_markdown

try:
    from ui.themes import list_themes, set_theme, get_theme_manager, get_current_theme
    HAS_THEMES = True
except ImportError:
    HAS_THEMES = False

TOOL_BOX_WIDTH = 100
MAX_TOOLS_PER_ITERATION = 50
RESULT_PREVIEW_LENGTH = 500
MAX_LINE_LENGTH = 95
COMPACT_KEEP_MESSAGES = 4
CACHE_TTL_MINUTES = 30
CACHE_MAX_SIZE = 100
RATE_LIMIT_CALLS = 60
RATE_LIMIT_WINDOW = 60
AUTOSAVE_INTERVAL = 10
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5
PLUGIN_DIR = Path("plugins")
METRICS_EXPORT_DIR = Path("metrics")
LOG_DIR = Path("logs")

C = Colors()


class AgentState(Enum):
    IDLE = auto()
    PROCESSING = auto()
    WAITING_TOOLS = auto()
    ERROR = auto()
    HEAVY_MODE = auto()
    PAUSED = auto()
    RECOVERING = auto()


class ToolExecutionStatus(Enum):
    PENDING = "⏳"
    SUCCESS = "✓"
    ERROR = "✗"
    SKIPPED = "⊘"
    TIMEOUT = "⏱"
    CANCELLED = "⊗"


class LogLevel(Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class CacheStrategy(Enum):
    LRU = auto()
    LFU = auto()
    TTL = auto()
    HYBRID = auto()


@dataclass
class StateTransition:
    from_state: AgentState
    to_state: AgentState
    timestamp: datetime = field(default_factory=datetime.now)
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolExecutionResult:
    tool_name: str
    success: bool
    result: str
    elapsed_time: float
    error_message: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0
    cancelled: bool = False
    timeout: bool = False
    
    @property
    def status(self) -> ToolExecutionStatus:
        if self.cancelled:
            return ToolExecutionStatus.CANCELLED
        if self.timeout:
            return ToolExecutionStatus.TIMEOUT
        if self.success:
            return ToolExecutionStatus.SUCCESS
        return ToolExecutionStatus.ERROR
    
    @property
    def result_preview(self) -> str:
        if len(self.result) > RESULT_PREVIEW_LENGTH:
            return self.result[:RESULT_PREVIEW_LENGTH] + "..."
        return self.result
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'tool_name': self.tool_name,
            'success': self.success,
            'result_length': len(self.result),
            'elapsed_time': self.elapsed_time,
            'error_message': self.error_message,
            'arguments': self.arguments,
            'retry_count': self.retry_count,
            'status': self.status.value
        }


@dataclass
class CommandResult:
    success: bool
    message: str = ""
    should_continue: bool = True
    data: Optional[Any] = None
    execution_time: float = 0.0


@dataclass
class CacheEntry:
    response: str
    timestamp: datetime
    access_count: int = 0
    last_accessed: datetime = field(default_factory=datetime.now)
    model_id: str = ""
    token_count: int = 0


@dataclass
class RateLimitInfo:
    calls_made: int
    window_start: datetime
    remaining: int
    reset_time: datetime


@dataclass
class AgentMetrics:
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_tokens: int = 0
    total_execution_time: float = 0
    tool_usage: Dict[str, int] = field(default_factory=dict)
    model_usage: Dict[str, int] = field(default_factory=dict)
    error_types: Dict[str, int] = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    rate_limit_waits: int = 0
    session_start: datetime = field(default_factory=datetime.now)
    last_request_time: Optional[datetime] = None
    average_response_time: float = 0.0
    peak_memory_usage: int = 0
    tool_execution_times: Dict[str, List[float]] = field(default_factory=dict)
    state_transitions: List[StateTransition] = field(default_factory=list)
    
    def record_request(self, success: bool, tokens: int, execution_time: float, model: str):
        self.total_requests += 1
        if success:
            self.successful_requests += 1
        else:
            self.failed_requests += 1
        
        self.total_tokens += tokens
        self.total_execution_time += execution_time
        self.model_usage[model] = self.model_usage.get(model, 0) + 1
        self.last_request_time = datetime.now()
        
        if self.total_requests > 0:
            self.average_response_time = self.total_execution_time / self.total_requests
    
    def record_tool_use(self, tool_name: str, execution_time: float = 0.0):
        self.tool_usage[tool_name] = self.tool_usage.get(tool_name, 0) + 1
        
        if tool_name not in self.tool_execution_times:
            self.tool_execution_times[tool_name] = []
        self.tool_execution_times[tool_name].append(execution_time)
    
    def record_error(self, error_type: str):
        self.error_types[error_type] = self.error_types.get(error_type, 0) + 1
    
    def record_cache_hit(self):
        self.cache_hits += 1
    
    def record_cache_miss(self):
        self.cache_misses += 1
    
    def record_rate_limit_wait(self):
        self.rate_limit_waits += 1
    
    def record_state_transition(self, transition: StateTransition):
        self.state_transitions.append(transition)
        if len(self.state_transitions) > 1000:
            self.state_transitions = self.state_transitions[-500:]
    
    def get_cache_hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return (self.cache_hits / total * 100) if total > 0 else 0.0
    
    def get_success_rate(self) -> float:
        return (self.successful_requests / self.total_requests * 100) if self.total_requests > 0 else 0.0
    
    def get_tool_avg_time(self, tool_name: str) -> float:
        times = self.tool_execution_times.get(tool_name, [])
        return sum(times) / len(times) if times else 0.0
    
    def get_session_duration(self) -> timedelta:
        return datetime.now() - self.session_start
    
    def get_summary(self) -> Dict[str, Any]:
        return {
            'total_requests': self.total_requests,
            'success_rate': f"{self.get_success_rate():.1f}%",
            'avg_execution_time': f"{self.average_response_time:.2f}s",
            'total_tokens': self.total_tokens,
            'cache_hit_rate': f"{self.get_cache_hit_rate():.1f}%",
            'rate_limit_waits': self.rate_limit_waits,
            'session_duration': str(self.get_session_duration()).split('.')[0],
            'top_tools': sorted(self.tool_usage.items(), key=lambda x: x[1], reverse=True)[:5],
            'top_errors': sorted(self.error_types.items(), key=lambda x: x[1], reverse=True)[:3],
            'model_distribution': self.model_usage
        }
    
    def export_to_json(self, filepath: Path):
        METRICS_EXPORT_DIR.mkdir(exist_ok=True)
        data = self.get_summary()
        data['exported_at'] = datetime.now().isoformat()
        data['raw_metrics'] = {
            'total_execution_time': self.total_execution_time,
            'tool_execution_times': {k: {'avg': sum(v)/len(v), 'count': len(v)} 
                                     for k, v in self.tool_execution_times.items() if v}
        }
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    
    def reset(self):
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.total_tokens = 0
        self.total_execution_time = 0
        self.tool_usage.clear()
        self.model_usage.clear()
        self.error_types.clear()
        self.cache_hits = 0
        self.cache_misses = 0
        self.rate_limit_waits = 0
        self.session_start = datetime.now()
        self.last_request_time = None
        self.average_response_time = 0.0
        self.tool_execution_times.clear()
        self.state_transitions.clear()


class StateMachine:
    def __init__(self, metrics: Optional[AgentMetrics] = None):
        self.current_state = AgentState.IDLE
        self.previous_state: Optional[AgentState] = None
        self.state_history: List[StateTransition] = []
        self.metrics = metrics
        self.lock = threading.Lock()
        
        self.valid_transitions = {
            AgentState.IDLE: {AgentState.PROCESSING, AgentState.HEAVY_MODE, AgentState.PAUSED},
            AgentState.PROCESSING: {AgentState.IDLE, AgentState.WAITING_TOOLS, AgentState.ERROR, AgentState.PAUSED},
            AgentState.WAITING_TOOLS: {AgentState.PROCESSING, AgentState.ERROR, AgentState.IDLE},
            AgentState.ERROR: {AgentState.IDLE, AgentState.RECOVERING},
            AgentState.HEAVY_MODE: {AgentState.IDLE, AgentState.ERROR, AgentState.PAUSED},
            AgentState.PAUSED: {AgentState.IDLE, AgentState.PROCESSING, AgentState.HEAVY_MODE},
            AgentState.RECOVERING: {AgentState.IDLE, AgentState.ERROR}
        }
        
        self.state_callbacks: Dict[AgentState, List[Callable]] = {state: [] for state in AgentState}
    
    def can_transition(self, new_state: AgentState) -> bool:
        return new_state in self.valid_transitions.get(self.current_state, set())
    
    def transition_to(self, new_state: AgentState, reason: str = "", metadata: Dict[str, Any] = None) -> bool:
        with self.lock:
            if not self.can_transition(new_state):
                return False
            
            transition = StateTransition(
                from_state=self.current_state,
                to_state=new_state,
                reason=reason,
                metadata=metadata or {}
            )
            
            self.previous_state = self.current_state
            self.current_state = new_state
            self.state_history.append(transition)
            
            if len(self.state_history) > 500:
                self.state_history = self.state_history[-250:]
            
            if self.metrics:
                self.metrics.record_state_transition(transition)
            
            for callback in self.state_callbacks[new_state]:
                try:
                    callback(transition)
                except Exception:
                    pass
            
            return True
    
    def force_state(self, new_state: AgentState, reason: str = ""):
        with self.lock:
            transition = StateTransition(
                from_state=self.current_state,
                to_state=new_state,
                reason=f"FORCED: {reason}"
            )
            self.previous_state = self.current_state
            self.current_state = new_state
            self.state_history.append(transition)
    
    def on_state(self, state: AgentState, callback: Callable):
        self.state_callbacks[state].append(callback)
    
    def is_idle(self) -> bool:
        return self.current_state == AgentState.IDLE
    
    def is_processing(self) -> bool:
        return self.current_state in {AgentState.PROCESSING, AgentState.WAITING_TOOLS, AgentState.HEAVY_MODE}
    
    def is_error(self) -> bool:
        return self.current_state == AgentState.ERROR
    
    def get_history(self, limit: int = 10) -> List[StateTransition]:
        return self.state_history[-limit:]
    
    def reset(self):
        with self.lock:
            self.current_state = AgentState.IDLE
            self.previous_state = None
            self.state_history.clear()


class ResponseCache:
    def __init__(self, ttl_minutes: int = CACHE_TTL_MINUTES, max_size: int = CACHE_MAX_SIZE,
                 strategy: CacheStrategy = CacheStrategy.HYBRID):
        self.cache: Dict[str, CacheEntry] = {}
        self.ttl = timedelta(minutes=ttl_minutes)
        self.max_size = max_size
        self.strategy = strategy
        self.lock = threading.Lock()
        self.stats = {'hits': 0, 'misses': 0, 'evictions': 0}
    
    def _hash_request(self, messages: List[Dict], model: str) -> str:
        relevant_messages = messages[-10:]
        content = json.dumps(relevant_messages, sort_keys=True) + model
        return hashlib.sha256(content.encode()).hexdigest()
    
    def _is_valid(self, entry: CacheEntry) -> bool:
        return datetime.now() - entry.timestamp < self.ttl
    
    def _evict(self):
        if not self.cache:
            return
        
        if self.strategy == CacheStrategy.LRU:
            oldest = min(self.cache.items(), key=lambda x: x[1].last_accessed)
            del self.cache[oldest[0]]
        elif self.strategy == CacheStrategy.LFU:
            least_used = min(self.cache.items(), key=lambda x: x[1].access_count)
            del self.cache[least_used[0]]
        elif self.strategy == CacheStrategy.TTL:
            expired = [k for k, v in self.cache.items() if not self._is_valid(v)]
            for k in expired:
                del self.cache[k]
            if len(self.cache) >= self.max_size:
                oldest = min(self.cache.items(), key=lambda x: x[1].timestamp)
                del self.cache[oldest[0]]
        else:
            expired = [k for k, v in self.cache.items() if not self._is_valid(v)]
            for k in expired:
                del self.cache[k]
            
            if len(self.cache) >= self.max_size:
                sorted_entries = sorted(
                    self.cache.items(),
                    key=lambda x: (x[1].access_count, x[1].last_accessed)
                )
                del self.cache[sorted_entries[0][0]]
        
        self.stats['evictions'] += 1
    
    def get(self, messages: List[Dict], model: str) -> Optional[str]:
        with self.lock:
            key = self._hash_request(messages, model)
            
            if key in self.cache:
                entry = self.cache[key]
                if self._is_valid(entry):
                    entry.access_count += 1
                    entry.last_accessed = datetime.now()
                    self.stats['hits'] += 1
                    return entry.response
                else:
                    del self.cache[key]
            
            self.stats['misses'] += 1
            return None
    
    def set(self, messages: List[Dict], model: str, response: str, token_count: int = 0):
        with self.lock:
            if len(self.cache) >= self.max_size:
                self._evict()
            
            key = self._hash_request(messages, model)
            self.cache[key] = CacheEntry(
                response=response,
                timestamp=datetime.now(),
                model_id=model,
                token_count=token_count
            )
    
    def invalidate(self, messages: List[Dict], model: str):
        with self.lock:
            key = self._hash_request(messages, model)
            if key in self.cache:
                del self.cache[key]
    
    def clear(self):
        with self.lock:
            self.cache.clear()
            self.stats = {'hits': 0, 'misses': 0, 'evictions': 0}
    
    def cleanup_expired(self):
        with self.lock:
            expired = [k for k, v in self.cache.items() if not self._is_valid(v)]
            for k in expired:
                del self.cache[k]
            return len(expired)
    
    def get_stats(self) -> Dict[str, Any]:
        total = self.stats['hits'] + self.stats['misses']
        hit_rate = (self.stats['hits'] / total * 100) if total > 0 else 0
        
        return {
            'size': len(self.cache),
            'max_size': self.max_size,
            'hits': self.stats['hits'],
            'misses': self.stats['misses'],
            'hit_rate': f"{hit_rate:.1f}%",
            'evictions': self.stats['evictions'],
            'strategy': self.strategy.name,
            'ttl_minutes': self.ttl.seconds // 60
        }


class RateLimiter:
    def __init__(self, max_calls: int = RATE_LIMIT_CALLS, window_seconds: int = RATE_LIMIT_WINDOW):
        self.max_calls = max_calls
        self.window = window_seconds
        self.calls: Deque[float] = deque()
        self.lock = threading.Lock()
        self.total_waits = 0
        self.total_wait_time = 0.0
    
    def _cleanup_old_calls(self):
        now = time.time()
        while self.calls and self.calls[0] < now - self.window:
            self.calls.popleft()
    
    def wait_if_needed(self) -> float:
        with self.lock:
            now = time.time()
            self._cleanup_old_calls()
            
            if len(self.calls) >= self.max_calls:
                wait_time = self.calls[0] + self.window - now
                
                if wait_time > 0:
                    self.total_waits += 1
                    self.total_wait_time += wait_time
                    
                    print(f"{C.YELLOW}⏳ Rate limit alcanzado. Esperando {wait_time:.1f}s...{C.RESET}")
                    time.sleep(wait_time)
                    
                    self._cleanup_old_calls()
                    self.calls.append(time.time())
                    return wait_time
            
            self.calls.append(now)
            return 0.0
    
    def get_remaining(self) -> int:
        with self.lock:
            self._cleanup_old_calls()
            return max(0, self.max_calls - len(self.calls))
    
    def get_reset_time(self) -> Optional[float]:
        with self.lock:
            if self.calls:
                return self.calls[0] + self.window
            return None
    
    def get_info(self) -> RateLimitInfo:
        with self.lock:
            self._cleanup_old_calls()
            reset_time = datetime.fromtimestamp(self.calls[0] + self.window) if self.calls else datetime.now()
            window_start = datetime.fromtimestamp(self.calls[0]) if self.calls else datetime.now()
            
            return RateLimitInfo(
                calls_made=len(self.calls),
                window_start=window_start,
                remaining=max(0, self.max_calls - len(self.calls)),
                reset_time=reset_time
            )
    
    def get_stats(self) -> Dict[str, Any]:
        info = self.get_info()
        avg_wait = self.total_wait_time / self.total_waits if self.total_waits > 0 else 0
        
        return {
            'calls_in_window': info.calls_made,
            'max_calls': self.max_calls,
            'window_seconds': self.window,
            'remaining': info.remaining,
            'usage_percent': (info.calls_made / self.max_calls * 100),
            'total_waits': self.total_waits,
            'total_wait_time': f"{self.total_wait_time:.1f}s",
            'avg_wait_time': f"{avg_wait:.2f}s"
        }
    
    def reset(self):
        with self.lock:
            self.calls.clear()
            self.total_waits = 0
            self.total_wait_time = 0.0


class ToolPlugin(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        pass
    
    @property
    def parameters(self) -> Dict[str, Any]:
        return {}
    
    @property
    def required_params(self) -> List[str]:
        return []
    
    @abstractmethod
    def execute(self, **kwargs) -> str:
        pass
    
    def validate(self, **kwargs) -> Tuple[bool, str]:
        for param in self.required_params:
            if param not in kwargs:
                return False, f"Parámetro requerido faltante: {param}"
        return True, ""
    
    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required_params
                }
            }
        }


class PluginLoader:
    def __init__(self, plugins_dir: Path = PLUGIN_DIR):
        self.plugins_dir = plugins_dir
        self.loaded_plugins: Dict[str, ToolPlugin] = {}
        self.plugin_errors: Dict[str, str] = {}
        self.load_times: Dict[str, float] = {}
    
    def discover_plugins(self) -> List[str]:
        if not self.plugins_dir.exists():
            self.plugins_dir.mkdir(parents=True)
            self._create_example_plugin()
            return []
        
        plugins = []
        for file in self.plugins_dir.glob("*.py"):
            if file.stem.startswith("_"):
                continue
            plugins.append(file.stem)
        
        return plugins
    
    def _create_example_plugin(self):
        example_content = '''
from typing import Dict, Any, List, Tuple

class ExampleTool:
    @property
    def name(self) -> str:
        return "example_tool"
    
    @property
    def description(self) -> str:
        return "Herramienta de ejemplo"
    
    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "input": {
                "type": "string",
                "description": "Entrada de ejemplo"
            }
        }
    
    @property
    def required_params(self) -> List[str]:
        return ["input"]
    
    def execute(self, **kwargs) -> str:
        return f"Resultado: {kwargs.get('input', '')}"
    
    def validate(self, **kwargs) -> Tuple[bool, str]:
        if 'input' not in kwargs:
            return False, "Falta input"
        return True, ""
'''
        example_file = self.plugins_dir / "_example_plugin.py"
        if not example_file.exists():
            with open(example_file, 'w', encoding='utf-8') as f:
                f.write(example_content)
    
    def load_plugin(self, plugin_name: str) -> Optional[ToolPlugin]:
        plugin_file = self.plugins_dir / f"{plugin_name}.py"
        
        if not plugin_file.exists():
            self.plugin_errors[plugin_name] = "Archivo no encontrado"
            return None
        
        start_time = time.time()
        
        try:
            spec = importlib.util.spec_from_file_location(plugin_name, plugin_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[plugin_name] = module
            spec.loader.exec_module(module)
            
            for attr_name in dir(module):
                if attr_name.startswith('_'):
                    continue
                    
                attr = getattr(module, attr_name)
                
                if isinstance(attr, type):
                    if hasattr(attr, 'name') and hasattr(attr, 'execute'):
                        try:
                            instance = attr()
                            if hasattr(instance, 'name') and callable(getattr(instance, 'execute', None)):
                                self.loaded_plugins[instance.name] = instance
                                self.load_times[plugin_name] = time.time() - start_time
                                return instance
                        except Exception as e:
                            self.plugin_errors[plugin_name] = f"Error instanciando: {e}"
                            continue
            
            self.plugin_errors[plugin_name] = "No se encontró clase válida de plugin"
            return None
            
        except Exception as e:
            self.plugin_errors[plugin_name] = str(e)
            return None
    
    def load_all(self) -> int:
        plugins = self.discover_plugins()
        loaded = 0
        
        for plugin in plugins:
            if self.load_plugin(plugin):
                loaded += 1
        
        return loaded
    
    def reload_plugin(self, plugin_name: str) -> bool:
        for name in list(self.loaded_plugins.keys()):
            plugin = self.loaded_plugins[name]
            if hasattr(plugin, '__module__') and plugin.__module__ == plugin_name:
                del self.loaded_plugins[name]
        
        if plugin_name in sys.modules:
            del sys.modules[plugin_name]
        
        return self.load_plugin(plugin_name) is not None
    
    def unload_plugin(self, plugin_name: str) -> bool:
        if plugin_name in self.loaded_plugins:
            del self.loaded_plugins[plugin_name]
            return True
        return False
    
    def get_plugin(self, name: str) -> Optional[ToolPlugin]:
        return self.loaded_plugins.get(name)
    
    def list_plugins(self) -> List[str]:
        return list(self.loaded_plugins.keys())
    
    def get_errors(self) -> Dict[str, str]:
        return self.plugin_errors.copy()
    
    def get_stats(self) -> Dict[str, Any]:
        return {
            'loaded': len(self.loaded_plugins),
            'errors': len(self.plugin_errors),
            'load_times': self.load_times,
            'plugins': list(self.loaded_plugins.keys())
        }


class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': C.DIM,
        'INFO': C.BRIGHT_CYAN,
        'WARNING': C.BRIGHT_YELLOW,
        'ERROR': C.BRIGHT_RED,
        'CRITICAL': C.BRIGHT_MAGENTA
    }
    
    def format(self, record):
        levelname = record.levelname
        if levelname in self.COLORS:
            record.levelname = f"{self.COLORS[levelname]}{levelname:8}{C.RESET}"
        return super().format(record)


class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_data = {
            'timestamp': datetime.now().isoformat(),
            'level': record.levelname,
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
            'message': record.getMessage()
        }
        
        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)
        
        if hasattr(record, 'extra_data'):
            log_data['data'] = record.extra_data
        
        return json.dumps(log_data, ensure_ascii=False)


class AgentLogger:
    def __init__(self, name: str = "nvidia_code", log_dir: Path = LOG_DIR,
                 console_level: str = "INFO", file_level: str = "DEBUG"):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers = []
        
        log_dir.mkdir(exist_ok=True)
        
        file_handler = RotatingFileHandler(
            log_dir / "agent.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding='utf-8'
        )
        file_handler.setLevel(getattr(logging, file_level.upper()))
        file_handler.setFormatter(JSONFormatter())
        
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, console_level.upper()))
        console_handler.setFormatter(ColoredFormatter('%(levelname)s │ %(message)s'))
        
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
        
        self.console_handler = console_handler
        self.file_handler = file_handler
    
    def set_console_level(self, level: str):
        self.console_handler.setLevel(getattr(logging, level.upper()))
    
    def set_file_level(self, level: str):
        self.file_handler.setLevel(getattr(logging, level.upper()))
    
    def debug(self, msg: str, **kwargs):
        self._log(logging.DEBUG, msg, **kwargs)
    
    def info(self, msg: str, **kwargs):
        self._log(logging.INFO, msg, **kwargs)
    
    def warning(self, msg: str, **kwargs):
        self._log(logging.WARNING, msg, **kwargs)
    
    def error(self, msg: str, **kwargs):
        self._log(logging.ERROR, msg, **kwargs)
    
    def critical(self, msg: str, **kwargs):
        self._log(logging.CRITICAL, msg, **kwargs)
    
    def _log(self, level: int, msg: str, **kwargs):
        extra = {'extra_data': kwargs} if kwargs else {}
        self.logger.log(level, msg, extra=extra)
    
    def exception(self, msg: str, **kwargs):
        self.logger.exception(msg, extra={'extra_data': kwargs})


class ToolDisplayManager:
    def __init__(self, width: int = TOOL_BOX_WIDTH):
        self.width = width
        self.current_tool: Optional[str] = None
        self.start_time: Optional[float] = None
    
    def _get_theme_colors(self) -> Tuple[str, str, str]:
        try:
            if HAS_THEMES:
                tm = get_theme_manager()
                primary = tm.rgb_to_ansi(tm.current_theme.primary)
                success = tm.rgb_to_ansi(tm.current_theme.success)
                error = tm.rgb_to_ansi(tm.current_theme.error)
                return primary, success, error
        except:
            pass
        return C.NVIDIA_GREEN, C.BRIGHT_GREEN, C.BRIGHT_RED
    
    def format_tool_name(self, name: str) -> str:
        return name.replace('_', ' ').title()
    
    def format_arguments(self, args: Dict[str, Any], max_args: int = 2) -> str:
        if not args:
            return ""
        
        formatted = []
        for key, value in list(args.items())[:max_args]:
            str_value = str(value)
            if len(str_value) > 25:
                str_value = str_value[:22] + "..."
            formatted.append(f"{key}={str_value}")
        
        result = " ".join(formatted)
        if len(args) > max_args:
            result += " ..."
        
        return f" {result}"
    
    def _calculate_visible_length(self, text: str) -> int:
        return len(re.sub(r'\x1b\[[0-9;]*m', '', text))
    
    def _pad_to_width(self, content: str, target_width: int) -> str:
        visible_len = self._calculate_visible_length(content)
        padding = max(0, target_width - visible_len)
        return content + ' ' * padding
    
    def print_box_start(self):
        print(f"\n{C.DIM}╭{'─' * self.width}╮{C.RESET}")
    
    def print_box_end(self):
        print(f"{C.DIM}╰{'─' * self.width}╯{C.RESET}\n")
    
    def print_box_separator(self):
        print(f"{C.DIM}├{'─' * self.width}┤{C.RESET}")
    
    def print_box_line(self, content: str, padding: bool = True):
        if padding:
            padded = self._pad_to_width(content, self.width - 2)
            print(f"{C.DIM}│{C.RESET} {padded}{C.DIM}│{C.RESET}")
        else:
            print(f"{C.DIM}│{C.RESET} {content}")
    
    def print_pending(self, tool_name: str, args: Dict[str, Any]):
        self.current_tool = tool_name
        self.start_time = time.time()
        
        display_name = self.format_tool_name(tool_name)
        args_str = self.format_arguments(args)
        primary, _, _ = self._get_theme_colors()
        tool_line = f"{display_name}{args_str}"
        print(f"{C.DIM}• running{C.RESET} {primary}{tool_line}{C.RESET}", flush=True)
    
    def print_success(self, tool_name: str, args: Dict[str, Any],
                      elapsed: float, result_size: int):
        display_name = self.format_tool_name(tool_name)
        args_str = self.format_arguments(args)
        tool_line = f"{display_name}{args_str}"
        
        size_str = f"{result_size:,}" if result_size > 1000 else str(result_size)
        status_text = f"({elapsed:.2f}s, {size_str} chars)"
        
        _, success_color, _ = self._get_theme_colors()
        print(f"{success_color}✓ done{C.RESET} {C.BRIGHT_CYAN}{tool_line}{C.RESET} {C.DIM}{status_text}{C.RESET}")
    
    def print_error(self, tool_name: str, args: Dict[str, Any], error_msg: str):
        display_name = self.format_tool_name(tool_name)
        args_str = self.format_arguments(args)
        tool_line = f"{display_name}{args_str}"
        
        if len(error_msg) > 60:
            error_msg = error_msg[:57] + "..."
        
        _, _, error_color = self._get_theme_colors()
        print(f"{error_color}✗ fail{C.RESET} {C.BRIGHT_CYAN}{tool_line}{C.RESET} {error_color}{error_msg}{C.RESET}")
    
    def print_timeout(self, tool_name: str, args: Dict[str, Any], timeout_seconds: float):
        display_name = self.format_tool_name(tool_name)
        args_str = self.format_arguments(args)
        tool_line = f"{display_name}{args_str}"
        
        print(f"{C.BRIGHT_YELLOW}⏱ timeout{C.RESET} {C.BRIGHT_CYAN}{tool_line}{C.RESET} {C.BRIGHT_YELLOW}({timeout_seconds}s){C.RESET}")
    
    def print_cancelled(self, tool_name: str, args: Dict[str, Any]):
        display_name = self.format_tool_name(tool_name)
        args_str = self.format_arguments(args)
        tool_line = f"{display_name}{args_str}"
        
        print(f"{C.DIM}⊗ cancel{C.RESET} {C.BRIGHT_CYAN}{tool_line}{C.RESET}")
    
    def print_retry(self, tool_name: str, attempt: int, max_attempts: int):
        content = f"{C.BRIGHT_YELLOW}↻ retry{C.RESET} {tool_name} ({attempt}/{max_attempts})"
        print(content)
    
    def print_result_preview(self, result: str, max_lines: int = 8):
        if len(result) < 100:
            return
        
        print(f"{C.DIM}│{C.RESET}")
        
        try:
            lines = result.split('\n')[:max_lines]
            
            for line in lines:
                if len(line) > MAX_LINE_LENGTH:
                    line = line[:MAX_LINE_LENGTH - 3] + "..."
                print(f"{C.DIM}  {line}{C.RESET}")
            
            if len(result.split('\n')) > max_lines:
                print(f"{C.DIM}  ... (preview){C.RESET}")
                
        except Exception:
            lines = result.split('\n')[:max_lines]
            for line in lines:
                if len(line) > MAX_LINE_LENGTH:
                    line = line[:MAX_LINE_LENGTH - 3] + "..."
                print(f"{C.DIM}  {line}{C.RESET}")
    
    def print_batch_header(self, total_tools: int):
        content = f"{C.BRIGHT_CYAN}tools{C.RESET} ejecutando {total_tools}..."
        print(f"\n{content}")
    
    def print_batch_summary(self, successful: int, failed: int, total_time: float):
        if failed == 0:
            status = f"{C.BRIGHT_GREEN}✓ Todas exitosas{C.RESET}"
        else:
            status = f"{C.BRIGHT_YELLOW}⚠ {successful} exitosas, {failed} fallidas{C.RESET}"
        
        content = f"{status} {C.DIM}({total_time:.2f}s total){C.RESET}\n"
        print(content)


class CommandCompleter:
    def __init__(self, commands: List[str]):
        self.commands = sorted(commands)
        self.matches: List[str] = []
    
    def complete(self, text: str, state: int) -> Optional[str]:
        if state == 0:
            if text:
                self.matches = [cmd for cmd in self.commands if cmd.startswith(text)]
            else:
                self.matches = self.commands[:]
        
        if state < len(self.matches):
            return self.matches[state]
        return None
    
    def update_commands(self, commands: List[str]):
        self.commands = sorted(commands)


class InputHandler:
    def __init__(self, command_handler: 'CommandHandler'):
        self.command_handler = command_handler
        self.history: List[str] = []
        self.history_file = Path.home() / ".nvidia_code_history"
        self.completer: Optional[CommandCompleter] = None
        
        self._setup_readline()
        self._load_history()
    
    def _setup_readline(self):
        if not HAS_READLINE:
            return
        
        try:
            commands = list(self.command_handler._commands.keys())
            self.completer = CommandCompleter(commands)
            
            readline.set_completer(self.completer.complete)
            readline.parse_and_bind('tab: complete')
            readline.set_completer_delims(' \t\n;')
            
            readline.set_history_length(1000)
        except Exception:
            pass
    
    def _load_history(self):
        if not HAS_READLINE:
            return
        
        try:
            if self.history_file.exists():
                readline.read_history_file(str(self.history_file))
        except Exception:
            pass
    
    def save_history(self):
        if not HAS_READLINE:
            return
        
        try:
            readline.write_history_file(str(self.history_file))
        except Exception:
            pass
    
    def add_to_history(self, line: str):
        self.history.append(line)
        if len(self.history) > 1000:
            self.history = self.history[-500:]
    
    def get_input(self, prompt: str) -> str:
        try:
            user_input = input(prompt).strip()
            if user_input:
                self.add_to_history(user_input)
            return user_input
        except EOFError:
            return "/exit"
        except KeyboardInterrupt:
            print()
            return ""
    
    def update_completer(self, commands: List[str]):
        if self.completer:
            self.completer.update_commands(commands)


class AutoSaveManager:
    def __init__(self, save_dir: Path = Path(".autosave"), interval: int = AUTOSAVE_INTERVAL):
        self.save_dir = save_dir
        self.interval = interval
        self.message_count = 0
        self.last_save_time = datetime.now()
        self.enabled = True
        
        self.save_dir.mkdir(exist_ok=True)
    
    def should_save(self) -> bool:
        if not self.enabled:
            return False
        return self.message_count >= self.interval
    
    def record_message(self):
        self.message_count += 1
    
    def save(self, messages: List[Dict], model_id: str, metadata: Dict[str, Any] = None) -> Optional[Path]:
        if not self.enabled:
            return None
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = self.save_dir / f"autosave_{timestamp}.json"
        
        try:
            data = {
                'timestamp': datetime.now().isoformat(),
                'model': model_id,
                'messages': messages,
                'metadata': metadata or {}
            }
            
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            self.message_count = 0
            self.last_save_time = datetime.now()
            
            self._cleanup_old_saves()
            
            return filename
        except Exception:
            return None
    
    def _cleanup_old_saves(self, keep: int = 10):
        try:
            saves = sorted(self.save_dir.glob("autosave_*.json"), reverse=True)
            for old_save in saves[keep:]:
                old_save.unlink()
        except Exception:
            pass
    
    def get_latest_save(self) -> Optional[Path]:
        try:
            saves = sorted(self.save_dir.glob("autosave_*.json"), reverse=True)
            return saves[0] if saves else None
        except Exception:
            return None
    
    def load_save(self, filepath: Path) -> Optional[Dict]:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None
    
    def list_saves(self) -> List[Dict[str, Any]]:
        saves = []
        try:
            for save_file in sorted(self.save_dir.glob("autosave_*.json"), reverse=True):
                try:
                    with open(save_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        saves.append({
                            'filename': save_file.name,
                            'path': save_file,
                            'timestamp': data.get('timestamp'),
                            'messages': len(data.get('messages', [])),
                            'model': data.get('model')
                        })
                except Exception:
                    continue
        except Exception:
            pass
        return saves


class JSONValidator:
    @staticmethod
    def parse(json_str: str) -> Tuple[Optional[Dict], Optional[str]]:
        if not json_str or not json_str.strip():
            return {}, None
        
        json_str = json_str.strip()
        
        try:
            return json.loads(json_str), None
        except json.JSONDecodeError:
            pass
        
        try:
            fixed = json_str.replace("'", '"')
            return json.loads(fixed), None
        except json.JSONDecodeError:
            pass
        
        try:
            fixed = re.sub(r',\s*}', '}', json_str)
            fixed = re.sub(r',\s*]', ']', fixed)
            return json.loads(fixed), None
        except json.JSONDecodeError:
            pass
        
        try:
            fixed = re.sub(r'(\w+):', r'"\1":', json_str)
            return json.loads(fixed), None
        except json.JSONDecodeError:
            pass
        
        try:
            result = JSONValidator._extract_key_values(json_str)
            if result:
                return result, None
        except Exception:
            pass
        
        return None, f"JSON inválido: {json_str[:50]}..."
    
    @staticmethod
    def _extract_key_values(text: str) -> Optional[Dict]:
        result = {}
        
        patterns = [
            r'"(\w+)"\s*:\s*"([^"]*)"',
            r'"(\w+)"\s*:\s*(\d+(?:\.\d+)?)',
            r'"(\w+)"\s*:\s*(true|false|null)',
            r"'(\w+)'\s*:\s*'([^']*)'",
            r'(\w+)\s*=\s*"([^"]*)"',
            r'(\w+)\s*=\s*([^\s,]+)',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for key, value in matches:
                if value.lower() == 'true':
                    result[key] = True
                elif value.lower() == 'false':
                    result[key] = False
                elif value.lower() == 'null':
                    result[key] = None
                else:
                    try:
                        result[key] = int(value)
                    except ValueError:
                        try:
                            result[key] = float(value)
                        except ValueError:
                            result[key] = value
        
        return result if result else None
    
    @staticmethod
    def sanitize(data: Dict) -> Dict:
        def clean_value(v):
            if isinstance(v, str):
                return v.strip()
            elif isinstance(v, dict):
                return {k: clean_value(val) for k, val in v.items()}
            elif isinstance(v, list):
                return [clean_value(item) for item in v]
            return v
        
        return {k: clean_value(v) for k, v in data.items()}


class ToolExecutor:
    def __init__(self, display: ToolDisplayManager, metrics: AgentMetrics,
                 logger: AgentLogger, max_retries: int = 3, timeout: float = 60.0):
        self.display = display
        self.metrics = metrics
        self.logger = logger
        self.max_retries = max_retries
        self.timeout = timeout
        self.cancelled = False
        self.execution_queue: queue.Queue = queue.Queue()
    
    def cancel_all(self):
        self.cancelled = True
        while not self.execution_queue.empty():
            try:
                self.execution_queue.get_nowait()
            except queue.Empty:
                break
    
    def reset(self):
        self.cancelled = False
    
    def execute(self, tool_call: Dict[str, Any], conversation: ConversationManager,
                plugin_loader: Optional[PluginLoader] = None) -> ToolExecutionResult:
        tool_name = tool_call.get('function', {}).get('name', '')
        tool_id = tool_call.get('id', f'call_{tool_name}_{int(time.time())}')
        
        if not tool_name:
            return ToolExecutionResult(
                tool_name="unknown",
                success=False,
                result="",
                elapsed_time=0,
                error_message="Nombre de herramienta vacío"
            )
        
        args_str = tool_call.get('function', {}).get('arguments', '{}')
        tool_args, parse_error = JSONValidator.parse(args_str)
        
        if parse_error:
            self.display.print_box_start()
            self.display.print_error(tool_name, {}, parse_error)
            self.display.print_box_end()
            
            conversation.add_tool_result(
                tool_call_id=tool_id,
                name=tool_name,
                content=f"[Error] {parse_error}"
            )
            
            self.logger.error(f"JSON parse error for {tool_name}", error=parse_error)
            
            return ToolExecutionResult(
                tool_name=tool_name,
                success=False,
                result="",
                elapsed_time=0,
                error_message=parse_error,
                arguments={}
            )
        
        tool_args = JSONValidator.sanitize(tool_args)
        
        has_builtin = ToolRegistry.has_tool(tool_name)
        has_plugin = plugin_loader and tool_name in plugin_loader.loaded_plugins
        
        if not has_builtin and not has_plugin:
            error_msg = f"Herramienta no encontrada: {tool_name}"
            self.display.print_box_start()
            self.display.print_error(tool_name, tool_args, error_msg)
            self.display.print_box_end()
            
            conversation.add_tool_result(
                tool_call_id=tool_id,
                name=tool_name,
                content=f"[Error] {error_msg}"
            )
            
            self.logger.warning(f"Tool not found: {tool_name}")
            
            return ToolExecutionResult(
                tool_name=tool_name,
                success=False,
                result="",
                elapsed_time=0,
                error_message=error_msg,
                arguments=tool_args
            )
        
        self.display.print_pending(tool_name, tool_args)
        
        result = self._execute_with_retry(
            tool_name, tool_args, has_builtin, plugin_loader
        )
        
        if result.success:
            self.display.print_success(
                tool_name, tool_args, result.elapsed_time, len(result.result)
            )
            
            if len(result.result) > 200:
                self.display.print_result_preview(result.result)
        elif result.timeout:
            self.display.print_timeout(tool_name, tool_args, self.timeout)
        elif result.cancelled:
            self.display.print_cancelled(tool_name, tool_args)
        else:
            self.display.print_error(tool_name, tool_args, result.error_message or "Error desconocido")
        
        self.display.print_box_end()
        
        conversation.add_tool_result(
            tool_call_id=tool_id,
            name=tool_name,
            content=result.result if result.success else f"[Error] {result.error_message}"
        )
        
        self.metrics.record_tool_use(tool_name, result.elapsed_time)
        
        if not result.success:
            self.metrics.record_error(f"tool_{tool_name}")
            self.logger.error(f"Tool execution failed: {tool_name}", 
                            error=result.error_message, args=tool_args)
        else:
            self.logger.debug(f"Tool executed: {tool_name}", 
                            elapsed=result.elapsed_time, result_size=len(result.result))
        
        return result
    
    def _execute_with_retry(self, tool_name: str, tool_args: Dict[str, Any],
                           use_builtin: bool, plugin_loader: Optional[PluginLoader]
                           ) -> ToolExecutionResult:
        last_error = ""
        
        for attempt in range(1, self.max_retries + 1):
            if self.cancelled:
                return ToolExecutionResult(
                    tool_name=tool_name,
                    success=False,
                    result="",
                    elapsed_time=0,
                    cancelled=True,
                    arguments=tool_args
                )
            
            if attempt > 1:
                self.display.print_retry(tool_name, attempt, self.max_retries)
                time.sleep(0.5 * attempt)
            
            start_time = time.time()
            
            try:
                if use_builtin:
                    tool_result = ToolRegistry.execute(tool_name, **tool_args)
                else:
                    plugin = plugin_loader.get_plugin(tool_name)
                    valid, validation_error = plugin.validate(**tool_args)
                    if not valid:
                        raise ValueError(validation_error)
                    tool_result = plugin.execute(**tool_args)
                
                elapsed = time.time() - start_time
                
                if not isinstance(tool_result, str):
                    tool_result = str(tool_result)
                
                is_error = tool_result.startswith("[x]") or tool_result.startswith("[Error]")
                
                if is_error:
                    error_preview = tool_result.replace("[x]", "").replace("[Error]", "").strip()[:60]
                    last_error = error_preview
                    
                    if attempt < self.max_retries:
                        continue
                    
                    return ToolExecutionResult(
                        tool_name=tool_name,
                        success=False,
                        result=tool_result,
                        elapsed_time=elapsed,
                        error_message=error_preview,
                        arguments=tool_args,
                        retry_count=attempt - 1
                    )
                
                return ToolExecutionResult(
                    tool_name=tool_name,
                    success=True,
                    result=tool_result,
                    elapsed_time=elapsed,
                    arguments=tool_args,
                    retry_count=attempt - 1
                )
                
            except TimeoutError:
                elapsed = time.time() - start_time
                return ToolExecutionResult(
                    tool_name=tool_name,
                    success=False,
                    result="",
                    elapsed_time=elapsed,
                    timeout=True,
                    arguments=tool_args,
                    retry_count=attempt - 1
                )
            
            except TypeError as e:
                elapsed = time.time() - start_time
                last_error = str(e)[:60]
                
                return ToolExecutionResult(
                    tool_name=tool_name,
                    success=False,
                    result="",
                    elapsed_time=elapsed,
                    error_message=last_error,
                    arguments=tool_args,
                    retry_count=attempt - 1
                )
            
            except Exception as e:
                elapsed = time.time() - start_time
                last_error = f"{type(e).__name__}: {str(e)[:50]}"
                
                if attempt >= self.max_retries:
                    return ToolExecutionResult(
                        tool_name=tool_name,
                        success=False,
                        result="",
                        elapsed_time=elapsed,
                        error_message=last_error,
                        arguments=tool_args,
                        retry_count=attempt - 1
                    )
        
        return ToolExecutionResult(
            tool_name=tool_name,
            success=False,
            result="",
            elapsed_time=0,
            error_message=last_error or "Max retries exceeded",
            arguments=tool_args,
            retry_count=self.max_retries
        )
    
    def execute_batch(self, tool_calls: List[Dict[str, Any]], 
                      conversation: ConversationManager,
                      plugin_loader: Optional[PluginLoader] = None,
                      parallel: bool = False) -> List[ToolExecutionResult]:
        results = []
        total_tools = len(tool_calls)
        
        if total_tools > 1:
            self.display.print_batch_header(total_tools)
        
        start_time = time.time()
        successful = 0
        failed = 0
        
        for tc in tool_calls:
            if self.cancelled:
                break
            
            result = self.execute(tc, conversation, plugin_loader)
            results.append(result)
            
            if result.success:
                successful += 1
            else:
                failed += 1
        
        total_time = time.time() - start_time
        
        if total_tools > 1:
            self.display.print_batch_summary(successful, failed, total_time)
        
        return results


class CommandHandler:
    def __init__(self, agent: 'NVIDIACodeAgent'):
        self.agent = agent
        self._commands: Dict[str, Dict] = {}
        self._aliases: Dict[str, str] = {}
        self._categories: Dict[str, List[str]] = {}
        self._register_commands()
    
    def _register_commands(self):
        self._register_category('General', [
            ('/help', self._cmd_help, ['/h', '/?'], "Mostrar esta ayuda"),
            ('/exit', self._cmd_exit, ['/quit', '/q'], "Salir del agente"),
            ('/clear', self._cmd_clear, ['/cls'], "Limpiar pantalla"),
            ('/version', self._cmd_version, ['/v'], "Mostrar versión"),
        ])
        
        self._register_category('Modelos', [
            ('/model', self._cmd_model, ['/m'], "Cambiar modelo actual"),
            ('/models', self._cmd_list_models, [], "Listar modelos disponibles"),
            ('/test', self._cmd_test, [], "Probar conexión con modelo"),
        ])
        
        self._register_category('Modos', [
            ('/heavy', self._cmd_heavy, [], "Activar/desactivar Heavy Agent"),
            ('/auto', self._cmd_auto, [], "Activar/desactivar modo automático"),
            ('/stream', self._cmd_stream, [], "Activar/desactivar streaming"),
        ])
        
        self._register_category('Conversación', [
            ('/reset', self._cmd_reset, [], "Reiniciar conversación"),
            ('/compact', self._cmd_compact, [], "Compactar historial"),
            ('/history', self._cmd_history, ['/hist'], "Ver historial"),
            ('/save', self._cmd_save, [], "Guardar conversación"),
            ('/load', self._cmd_load, [], "Cargar conversación"),
            ('/autosave', self._cmd_autosave, [], "Gestionar auto-guardado"),
        ])
        
        self._register_category('Chats', [
            ('/chat list', self._cmd_chat_list, ['/chats'], "Listar chats guardados"),
            ('/save chat', self._cmd_chat_save, [], "Guardar chat con nombre"),
            ('/resume chat', self._cmd_chat_resume, ['/load chat'], "Retomar chat guardado"),
            ('/delete chat', self._cmd_chat_delete, [], "Eliminar chat guardado"),
            ('/search chat', self._cmd_chat_search, [], "Buscar chats"),
        ])
        
        self._register_category('Sistema', [
            ('/status', self._cmd_status, ['/info'], "Ver estado del agente"),
            ('/cd', self._cmd_cd, [], "Cambiar directorio de trabajo"),
            ('/pwd', self._cmd_pwd, [], "Mostrar directorio actual"),
            ('/ls', self._cmd_ls, ['/dir'], "Listar archivos"),
            ('/env', self._cmd_env, [], "Ver variables de entorno"),
        ])
        
        self._register_category('Herramientas', [
            ('/tools', self._cmd_tools, [], "Ver herramientas disponibles"),
            ('/tool', self._cmd_tool_info, [], "Info de una herramienta"),
        ])
        
        self._register_category('Plugins', [
            ('/plugins', self._cmd_plugins, [], "Gestionar plugins"),
            ('/plugin reload', self._cmd_plugin_reload, [], "Recargar un plugin"),
            ('/plugin info', self._cmd_plugin_info, [], "Info de un plugin"),
        ])
        
        self._register_category('Caché', [
            ('/cache', self._cmd_cache, [], "Gestionar caché de respuestas"),
            ('/cache clear', self._cmd_cache_clear, [], "Limpiar caché"),
            ('/cache stats', self._cmd_cache_stats, [], "Estadísticas de caché"),
        ])
        
        self._register_category('Rate Limit', [
            ('/ratelimit', self._cmd_ratelimit, ['/rl'], "Ver estado de rate limiting"),
            ('/ratelimit reset', self._cmd_ratelimit_reset, [], "Resetear rate limiter"),
        ])
        
        self._register_category('Visual', [
            ('/themes', self._cmd_themes, ['/theme'], "Gestionar temas"),
            ('/logo', self._cmd_logo, [], "Cambiar estilo de logo"),
            ('/colors', self._cmd_colors, [], "Mostrar paleta de colores"),
        ])
        
        self._register_category('Métricas', [
            ('/metrics', self._cmd_metrics, [], "Ver métricas del agente"),
            ('/metrics export', self._cmd_metrics_export, [], "Exportar métricas"),
            ('/metrics reset', self._cmd_metrics_reset, [], "Resetear métricas"),
        ])
        
        self._register_category('Logging', [
            ('/log', self._cmd_log, [], "Gestionar nivel de logging"),
            ('/log level', self._cmd_log_level, [], "Cambiar nivel de log"),
        ])
        
        self._register_category('Debug', [
            ('/debug', self._cmd_debug, [], "Modo debug"),
            ('/stats', self._cmd_stats, [], "Estadísticas detalladas"),
            ('/state', self._cmd_state, [], "Ver estado de la máquina de estados"),
        ])
    
    def _register_category(self, category: str, commands: List[Tuple]):
        self._categories[category] = []
        
        for cmd_tuple in commands:
            if len(cmd_tuple) == 4:
                command, handler, aliases, description = cmd_tuple
            else:
                command, handler, aliases = cmd_tuple
                description = ""
            
            self._categories[category].append(command)
            self.register(command, handler, aliases, description)
    
    def register(self, command: str, handler: Callable, 
                 aliases: List[str] = None, description: str = ""):
        self._commands[command] = {
            'handler': handler,
            'description': description,
            'aliases': aliases or []
        }
        
        for alias in (aliases or []):
            self._aliases[alias] = command
    
    def execute(self, command_str: str) -> CommandResult:
        start_time = time.time()
        
        parts = command_str.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        
        if cmd in self._aliases:
            cmd = self._aliases[cmd]
        
        full_cmd = f"{cmd} {args.split()[0]}" if args else cmd
        if full_cmd in self._commands:
            cmd = full_cmd
            args = " ".join(args.split()[1:]) if args else ""
        
        if cmd in self._commands:
            try:
                result = self._commands[cmd]['handler'](args)
                result.execution_time = time.time() - start_time
                return result
            except Exception as e:
                self.agent.logger.exception(f"Error executing command: {cmd}")
                return CommandResult(
                    success=False,
                    message=f"Error ejecutando comando: {e}",
                    execution_time=time.time() - start_time
                )
        
        return CommandResult(
            success=False,
            message=f"Comando no reconocido: {cmd}. Usa /help para ver comandos.",
            execution_time=time.time() - start_time
        )
    
    def get_commands_list(self) -> List[str]:
        return list(self._commands.keys())

    def _cmd_help(self, args: str) -> CommandResult:
        if args:
            cmd = args if args.startswith('/') else f'/{args}'
            if cmd in self._aliases:
                cmd = self._aliases[cmd]
            
            if cmd in self._commands:
                info = self._commands[cmd]
                print(f"\n{C.NVIDIA_GREEN}═══ {cmd} ═══{C.RESET}")
                print(f"{C.DIM}Descripción:{C.RESET} {info['description']}")
                if info['aliases']:
                    print(f"{C.DIM}Aliases:{C.RESET} {', '.join(info['aliases'])}")
                print()
                return CommandResult(success=True)
            else:
                return CommandResult(success=False, message=f"Comando no encontrado: {cmd}")
        
        width = 70
        print(f"\n{C.NVIDIA_GREEN}╔{'═' * width}╗{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET} {C.BOLD}{C.BRIGHT_WHITE}📚 COMANDOS DISPONIBLES{C.RESET}{' ' * (width - 25)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}╠{'═' * width}╣{C.RESET}")
        
        for category, cmds in self._categories.items():
            cat_line = f" {C.BRIGHT_YELLOW}▸ {category}{C.RESET}"
            padding = width - len(category) - 4
            print(f"{C.NVIDIA_GREEN}║{C.RESET}{cat_line}{' ' * padding}{C.NVIDIA_GREEN}║{C.RESET}")
            
            for cmd in cmds:
                if cmd in self._commands:
                    info = self._commands[cmd]
                    aliases = f" ({', '.join(info['aliases'])})" if info['aliases'] else ""
                    desc = info['description'][:35]
                    
                    cmd_display = f"{C.BRIGHT_CYAN}{cmd:15}{C.RESET}"
                    alias_display = f"{C.DIM}{aliases:12}{C.RESET}"
                    
                    content = f"  {cmd_display}{alias_display} {desc}"
                    print(f"{C.NVIDIA_GREEN}║{C.RESET}{content}{' ' * 5}{C.NVIDIA_GREEN}║{C.RESET}")
            
            print(f"{C.NVIDIA_GREEN}║{C.RESET}{' ' * width}{C.NVIDIA_GREEN}║{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}╠{'═' * width}╣{C.RESET}")
        tip = f" {C.DIM}💡 Usa /help <comando> para más detalles{C.RESET}"
        print(f"{C.NVIDIA_GREEN}║{C.RESET}{tip}{' ' * (width - 43)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}╚{'═' * width}╝{C.RESET}\n")
        
        return CommandResult(success=True)
    
    def _cmd_exit(self, args: str) -> CommandResult:
        self.agent.autosave_manager.save(
            [asdict(m) for m in self.agent.conversation.messages],
            self.agent.current_model.id,
            {'exit': True}
        )
        
        self.agent.input_handler.save_history()
        
        print(f"\n{C.NVIDIA_GREEN}╔{'═' * 45}╗{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET} {C.BOLD}👋 ¡Hasta luego!{C.RESET}{' ' * 26}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET} {C.DIM}Gracias por usar NVIDIA Code{C.RESET}{' ' * 14}{C.NVIDIA_GREEN}║{C.RESET}")
        
        duration = self.agent.metrics.get_session_duration()
        dur_str = str(duration).split('.')[0]
        print(f"{C.NVIDIA_GREEN}║{C.RESET} {C.DIM}Sesión: {dur_str}{C.RESET}{' ' * (34 - len(dur_str))}{C.NVIDIA_GREEN}║{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}╚{'═' * 45}╝{C.RESET}\n")
        
        sys.exit(0)
    
    def _cmd_clear(self, args: str) -> CommandResult:
        os.system('cls' if os.name == 'nt' else 'clear')
        return CommandResult(success=True, message="Pantalla limpiada")
    
    def _cmd_version(self, args: str) -> CommandResult:
        print(f"\n{C.NVIDIA_GREEN}NVIDIA Code{C.RESET} v2.0.0")
        print(f"{C.DIM}Python {sys.version.split()[0]} | {sys.platform}{C.RESET}")
        print(f"{C.DIM}Plugins cargados: {len(self.agent.plugin_loader.loaded_plugins)}{C.RESET}")
        print()
        return CommandResult(success=True)
    
    def _cmd_model(self, args: str) -> CommandResult:
        if not args:
            return self._cmd_list_models("")
        
        model = self.agent.registry.get(args.strip())
        if model:
            self.agent.current_model = model
            self.agent.system_prompt = self.agent._build_system_prompt()
            self.agent.response_cache.clear()
            
            print(f"\n{C.BRIGHT_GREEN}✅ Modelo cambiado:{C.RESET}")
            print(f"   {C.BRIGHT_CYAN}{model.name}{C.RESET} {model.specialty}")
            
            capabilities = []
            if model.supports_tools:
                capabilities.append(f"{C.BRIGHT_GREEN}🔧 Herramientas{C.RESET}")
            if model.thinking:
                capabilities.append(f"{C.BRIGHT_MAGENTA}🧠 Thinking{C.RESET}")
            
            if capabilities:
                print(f"   {' | '.join(capabilities)}")
            elif not model.supports_tools:
                print(f"   {C.BRIGHT_YELLOW}⚠️  Sin soporte de herramientas{C.RESET}")
            
            print()
            
            self.agent.logger.info(f"Model changed to {model.name}")
            
            return CommandResult(success=True)
        
        return CommandResult(
            success=False,
            message=f"Modelo no válido: {args}. Usa /models para ver la lista."
        )
    
    def _cmd_list_models(self, args: str) -> CommandResult:
        width = 75
        print(f"\n{C.NVIDIA_GREEN}╔{'═' * width}╗{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET} {C.BOLD}{C.BRIGHT_WHITE}🤖 MODELOS DISPONIBLES{C.RESET}{' ' * (width - 24)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}╠{'═' * width}╣{C.RESET}")
        
        for key, model in AVAILABLE_MODELS.items():
            current = " ◄" if model.id == self.agent.current_model.id else ""
            thinking = "🧠" if model.thinking else "  "
            tools = "🔧" if model.supports_tools else "  "
            
            name_display = f"{model.name[:28]:<28}"
            specialty_display = f"{model.specialty[:22]:<22}"
            
            line = f"  {C.BRIGHT_CYAN}{key:3}{C.RESET} [{thinking}{tools}] {name_display} {C.DIM}{specialty_display}{C.RESET}{C.GREEN}{current}{C.RESET}"
            
            print(f"{C.NVIDIA_GREEN}║{C.RESET}{line}{' ' * 5}{C.NVIDIA_GREEN}║{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}╠{'═' * width}╣{C.RESET}")
        legend = f" {C.DIM}🧠 = Thinking Mode  🔧 = Herramientas  |  Usa: /model <número>{C.RESET}"
        print(f"{C.NVIDIA_GREEN}║{C.RESET}{legend}{' ' * 8}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}╚{'═' * width}╝{C.RESET}\n")
        
        return CommandResult(success=True)
    
    def _cmd_test(self, args: str) -> CommandResult:
        model = self.agent.current_model
        print(f"\n{C.DIM}🔍 Probando conexión con {model.name}...{C.RESET}")
        
        start = time.time()
        
        wait_time = self.agent.rate_limiter.wait_if_needed()
        if wait_time > 0:
            self.agent.metrics.record_rate_limit_wait()
        
        result = self.agent.api_client.test_connection(model)
        elapsed = time.time() - start
        
        if result.get("success"):
            print(f"{C.BRIGHT_GREEN}✅ Conexión exitosa{C.RESET}")
            print(f"   {C.DIM}Tiempo de respuesta: {elapsed:.2f}s{C.RESET}")
            print(f"   {C.DIM}Modelo: {model.id}{C.RESET}")
            
            rate_info = self.agent.rate_limiter.get_info()
            print(f"   {C.DIM}Rate limit restante: {rate_info.remaining}/{self.agent.rate_limiter.max_calls}{C.RESET}")
            print()
            
            self.agent.logger.info(f"Connection test successful for {model.name}", elapsed=elapsed)
            
            return CommandResult(success=True)
        else:
            error = result.get('error', 'Error desconocido')
            print(f"{C.BRIGHT_RED}❌ Error de conexión{C.RESET}")
            print(f"   {C.DIM}{error}{C.RESET}\n")
            
            self.agent.logger.error(f"Connection test failed for {model.name}", error=error)
            
            return CommandResult(success=False, message=error)
    
    def _cmd_heavy(self, args: str) -> CommandResult:
        self.agent.heavy_mode = not self.agent.heavy_mode
        
        if self.agent.heavy_mode:
            print(f"\n{C.BRIGHT_MAGENTA}🔥 Heavy Agent ACTIVADO{C.RESET}")
            print(f"   {C.DIM}Modo colaborativo multi-IA habilitado{C.RESET}")
            print(f"   {C.DIM}Las respuestas pueden tardar más pero serán más precisas{C.RESET}\n")
        else:
            print(f"\n{C.DIM}⚡ Heavy Agent DESACTIVADO{C.RESET}")
            print(f"   {C.DIM}Modo estándar restaurado{C.RESET}\n")
        
        self.agent.logger.info(f"Heavy mode {'enabled' if self.agent.heavy_mode else 'disabled'}")
        
        return CommandResult(success=True)
    
    def _cmd_auto(self, args: str) -> CommandResult:
        self.agent.auto_mode = not self.agent.auto_mode
        
        status = "ACTIVADO" if self.agent.auto_mode else "DESACTIVADO"
        icon = "🤖" if self.agent.auto_mode else "👤"
        color = C.BRIGHT_GREEN if self.agent.auto_mode else C.DIM
        
        print(f"\n{color}{icon} Modo Automático {status}{C.RESET}")
        if self.agent.auto_mode:
            print(f"   {C.DIM}El agente ejecutará herramientas sin confirmación{C.RESET}\n")
        else:
            print(f"   {C.DIM}Se pedirá confirmación para operaciones sensibles{C.RESET}\n")
        
        self.agent.logger.info(f"Auto mode {'enabled' if self.agent.auto_mode else 'disabled'}")
        
        return CommandResult(success=True)
    
    def _cmd_stream(self, args: str) -> CommandResult:
        self.agent.stream = not self.agent.stream
        
        status = "ACTIVADO" if self.agent.stream else "DESACTIVADO"
        print(f"\n{C.BRIGHT_CYAN}📡 Streaming {status}{C.RESET}\n")
        
        return CommandResult(success=True)
    
    def _cmd_reset(self, args: str) -> CommandResult:
        msg_count = len(self.agent.conversation)
        self.agent.conversation.clear()
        self.agent.response_cache.clear()
        
        print(f"\n{C.BRIGHT_GREEN}✅ Conversación reiniciada{C.RESET}")
        print(f"   {C.DIM}{msg_count} mensajes eliminados{C.RESET}")
        print(f"   {C.DIM}Caché limpiado{C.RESET}\n")
        
        self.agent.logger.info(f"Conversation reset, {msg_count} messages deleted")
        
        return CommandResult(success=True)
    
    def _cmd_compact(self, args: str) -> CommandResult:
        keep = int(args) if args.isdigit() else COMPACT_KEEP_MESSAGES
        before = len(self.agent.conversation)
        
        self.agent.conversation.compact(keep_last=keep)
        after = len(self.agent.conversation)
        
        print(f"\n{C.BRIGHT_GREEN}✅ Historial compactado{C.RESET}")
        print(f"   {C.DIM}{before} → {after} mensajes (guardados últimos {keep}){C.RESET}\n")
        
        self.agent.logger.info(f"Conversation compacted: {before} -> {after} messages")
        
        return CommandResult(success=True)
    
    def _cmd_history(self, args: str) -> CommandResult:
        messages = self.agent.conversation.messages
        
        if not messages:
            print(f"\n{C.DIM}No hay mensajes en el historial{C.RESET}\n")
            return CommandResult(success=True)
        
        limit = int(args) if args.isdigit() else 10
        messages_to_show = messages[-limit:]
        
        print(f"\n{C.NVIDIA_GREEN}═══ Historial ({len(messages)} mensajes, mostrando {len(messages_to_show)}) ═══{C.RESET}\n")
        
        for i, msg in enumerate(messages_to_show):
            role = msg.role if hasattr(msg, 'role') else msg.get('role', 'unknown')
            content = (msg.content if hasattr(msg, 'content') else msg.get('content', ''))[:100]
            
            if role == 'user':
                icon = "👤"
                color = C.BRIGHT_CYAN
            elif role == 'assistant':
                icon = "🤖"
                color = C.BRIGHT_GREEN
            elif role == 'tool':
                icon = "🔧"
                color = C.BRIGHT_YELLOW
            else:
                icon = "❓"
                color = C.DIM
            
            full_content = msg.content if hasattr(msg, 'content') else msg.get('content', '')
            ellipsis = '...' if len(full_content) > 100 else ''
            print(f"{color}{icon} [{role:9}]{C.RESET} {content}{ellipsis}")
        
        print()
        return CommandResult(success=True)
    
    def _cmd_save(self, args: str) -> CommandResult:
        filename = args.strip() or f"conversation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        if not filename.endswith('.json'):
            filename += '.json'
        
        try:
            messages_data = []
            for m in self.agent.conversation.messages:
                if hasattr(m, '__dict__'):
                    messages_data.append(asdict(m) if hasattr(m, '__dataclass_fields__') else vars(m))
                else:
                    messages_data.append(m)
            
            data = {
                'model': self.agent.current_model.id,
                'timestamp': datetime.now().isoformat(),
                'messages': messages_data,
                'heavy_mode': self.agent.heavy_mode,
                'metrics_summary': self.agent.metrics.get_summary()
            }
            
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            
            print(f"\n{C.BRIGHT_GREEN}✅ Conversación guardada en: {filename}{C.RESET}\n")
            
            self.agent.logger.info(f"Conversation saved to {filename}")
            
            return CommandResult(success=True)
            
        except Exception as e:
            self.agent.logger.exception(f"Error saving conversation to {filename}")
            return CommandResult(success=False, message=f"Error al guardar: {e}")
    
    def _cmd_load(self, args: str) -> CommandResult:
        filename = args.strip()
        
        if not filename:
            json_files = list(Path('.').glob('conversation_*.json'))
            autosaves = self.agent.autosave_manager.list_saves()
            
            if json_files or autosaves:
                print(f"\n{C.NVIDIA_GREEN}Archivos disponibles:{C.RESET}")
                
                if json_files:
                    print(f"\n  {C.BRIGHT_CYAN}Guardados manualmente:{C.RESET}")
                    for f in sorted(json_files)[-5:]:
                        print(f"    • {f.name}")
                
                if autosaves:
                    print(f"\n  {C.BRIGHT_YELLOW}Auto-guardados:{C.RESET}")
                    for save in autosaves[:5]:
                        print(f"    • {save['filename']} ({save['messages']} msgs)")
                
                print(f"\n{C.DIM}Uso: /load <archivo>{C.RESET}\n")
            else:
                print(f"\n{C.DIM}No hay conversaciones guardadas{C.RESET}\n")
            return CommandResult(success=True)
        
        if not filename.endswith('.json'):
            filename += '.json'
        
        filepath = Path(filename)
        
        if not filepath.exists():
            autosave_path = self.agent.autosave_manager.save_dir / filename
            if autosave_path.exists():
                filepath = autosave_path
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            messages_data = data.get('messages', [])
            
            self.agent.conversation.messages = []
            for m_data in messages_data:
                if isinstance(m_data, dict):
                    msg = Message(**m_data)
                    self.agent.conversation.messages.append(msg)
            
            if 'heavy_mode' in data:
                self.agent.heavy_mode = data['heavy_mode']
            
            print(f"\n{C.BRIGHT_GREEN}✅ Conversación cargada: {filepath.name}{C.RESET}")
            print(f"   {C.DIM}{len(self.agent.conversation)} mensajes{C.RESET}\n")
            
            self.agent.logger.info(f"Conversation loaded from {filepath}")
            
            return CommandResult(success=True)
            
        except FileNotFoundError:
            return CommandResult(success=False, message=f"Archivo no encontrado: {filename}")
        except json.JSONDecodeError as e:
            return CommandResult(success=False, message=f"Error de formato JSON: {e}")
        except Exception as e:
            self.agent.logger.exception(f"Error loading conversation from {filename}")
            return CommandResult(success=False, message=f"Error: {e}")
    
    def _cmd_autosave(self, args: str) -> CommandResult:
        parts = args.split()
        
        if not parts:
            status = "activado" if self.agent.autosave_manager.enabled else "desactivado"
            interval = self.agent.autosave_manager.interval
            
            print(f"\n{C.NVIDIA_GREEN}💾 Auto-guardado:{C.RESET}")
            print(f"   Estado: {C.BRIGHT_GREEN if self.agent.autosave_manager.enabled else C.DIM}{status}{C.RESET}")
            print(f"   Intervalo: cada {interval} mensajes")
            print(f"   Mensajes desde último guardado: {self.agent.autosave_manager.message_count}")
            
            saves = self.agent.autosave_manager.list_saves()
            if saves:
                print(f"\n   {C.BRIGHT_CYAN}Últimos guardados:{C.RESET}")
                for save in saves[:3]:
                    print(f"     • {save['filename']} ({save['messages']} msgs)")
            
            print(f"\n   {C.DIM}Comandos: /autosave [on|off|now|list]{C.RESET}\n")
            return CommandResult(success=True)
        
        cmd = parts[0].lower()
        
        if cmd == "on":
            self.agent.autosave_manager.enabled = True
            print(f"\n{C.BRIGHT_GREEN}✅ Auto-guardado activado{C.RESET}\n")
        elif cmd == "off":
            self.agent.autosave_manager.enabled = False
            print(f"\n{C.DIM}Auto-guardado desactivado{C.RESET}\n")
        elif cmd == "now":
            filepath = self.agent.autosave_manager.save(
                [asdict(m) for m in self.agent.conversation.messages],
                self.agent.current_model.id
            )
            if filepath:
                print(f"\n{C.BRIGHT_GREEN}✅ Guardado: {filepath.name}{C.RESET}\n")
            else:
                return CommandResult(success=False, message="Error al guardar")
        elif cmd == "list":
            saves = self.agent.autosave_manager.list_saves()
            if saves:
                print(f"\n{C.NVIDIA_GREEN}💾 Auto-guardados:{C.RESET}")
                for save in saves:
                    print(f"   • {save['filename']} - {save['messages']} msgs - {save['model']}")
                print()
            else:
                print(f"\n{C.DIM}No hay auto-guardados{C.RESET}\n")
        else:
            return CommandResult(success=False, message="Comando no válido. Usa: on, off, now, list")
        
        return CommandResult(success=True)
    
    def _cmd_chat_list(self, args: str) -> CommandResult:
        chats = ChatStorage.list_chats()
        
        if not chats:
            print(f"\n{C.DIM}No hay chats guardados.{C.RESET}\n")
            return CommandResult(success=True)
        
        width = 75
        print(f"\n{C.NVIDIA_GREEN}╔{'═' * width}╗{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET} {C.BOLD}{C.BRIGHT_WHITE}📂 CHATS GUARDADOS ({len(chats)}){C.RESET}{' ' * (width - 24 - len(str(len(chats))))}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}╠{'═' * width}╣{C.RESET}")
        
        for i, chat in enumerate(chats):
            name = chat.name[:25]
            date = chat.last_modified_formatted
            msgs = f"{chat.message_count} msgs"
            model = chat.model[:15]
            
            line = f"  {C.BRIGHT_CYAN}{name:<25}{C.RESET} {C.DIM}{date:<18}{C.RESET} {C.BRIGHT_GREEN}{msgs:<10}{C.RESET} {C.DIM}{model}{C.RESET}"
            
            print(f"{C.NVIDIA_GREEN}║{C.RESET}{line}{' ' * 5}{C.NVIDIA_GREEN}║{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}╠{'═' * width}╣{C.RESET}")
        tip = f" {C.DIM}Usa /resume chat <nombre> para cargar un chat{C.RESET}"
        print(f"{C.NVIDIA_GREEN}║{C.RESET}{tip}{' ' * (width - 47)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}╚{'═' * width}╝{C.RESET}\n")
        
        return CommandResult(success=True)

    def _cmd_chat_save(self, args: str) -> CommandResult:
        name = args.strip()
        if not name:
            name = f"chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        messages_dict = [asdict(m) for m in self.agent.conversation.messages]
            
        success = ChatStorage.save_chat(
            name=name,
            messages=messages_dict,
            model_id=self.agent.current_model.id,
            working_directory=str(self.agent.working_directory),
            heavy_mode=self.agent.heavy_mode,
            stream_enabled=self.agent.stream
        )
        
        if success:
            print(f"\n{C.BRIGHT_GREEN}✅ Chat guardado como: {C.BRIGHT_WHITE}{name}{C.RESET}\n")
            self.agent.logger.info(f"Chat saved: {name}")
            return CommandResult(success=True)
        else:
            return CommandResult(success=False, message="Error al guardar el chat")

    def _cmd_chat_resume(self, args: str) -> CommandResult:
        name = args.strip()
        if not name:
            return CommandResult(success=False, message="Debes especificar el nombre del chat: /resume chat <nombre>")
            
        chat_data = ChatStorage.load_chat(name)
        if not chat_data:
            matches = ChatStorage.search_chats(name)
            if matches:
                name = matches[0].name
                chat_data = ChatStorage.load_chat(name)
            
        if chat_data:
            messages = []
            for m_dict in chat_data.messages:
                messages.append(Message(**m_dict))
                
            self.agent.conversation.messages = messages
            self.agent.heavy_mode = chat_data.heavy_mode
            self.agent.stream = chat_data.stream_enabled
            
            if chat_data.working_directory:
                try:
                    os.chdir(chat_data.working_directory)
                    self.agent.working_directory = Path(chat_data.working_directory)
                except:
                    pass
            
            print(f"\n{C.BRIGHT_GREEN}✅ Chat '{name}' cargado correctamente{C.RESET}")
            print(f"   {C.DIM}{len(messages)} mensajes restaurados{C.RESET}\n")
            
            self.agent.logger.info(f"Chat resumed: {name}")
            
            return CommandResult(success=True)
        else:
            return CommandResult(success=False, message=f"No se encontró el chat: {name}")

    def _cmd_chat_delete(self, args: str) -> CommandResult:
        name = args.strip()
        if not name:
            return CommandResult(success=False, message="Especifica el nombre del chat a eliminar")
            
        if ChatStorage.delete_chat(name):
            print(f"\n{C.BRIGHT_GREEN}✅ Chat '{name}' eliminado{C.RESET}\n")
            self.agent.logger.info(f"Chat deleted: {name}")
            return CommandResult(success=True)
        else:
            return CommandResult(success=False, message=f"No se pudo eliminar el chat: {name}")

    def _cmd_chat_search(self, args: str) -> CommandResult:
        query = args.strip()
        if not query:
            return CommandResult(success=False, message="Especifica un término de búsqueda")
            
        results = ChatStorage.search_chats(query)
        if not results:
            print(f"\n{C.DIM}No se encontraron chats que coincidan con '{query}'{C.RESET}\n")
            return CommandResult(success=True)
            
        print(f"\n{C.NVIDIA_GREEN}🔍 Resultados para '{query}':{C.RESET}")
        for chat in results:
            print(f"  • {C.BRIGHT_CYAN}{chat.name:25}{C.RESET} {C.DIM}{chat.last_modified_formatted}{C.RESET}")
        print()
        return CommandResult(success=True)
    
    def _cmd_status(self, args: str) -> CommandResult:
        stats = self.agent.conversation.get_stats()
        cache_stats = self.agent.response_cache.get_stats()
        rate_stats = self.agent.rate_limiter.get_stats()
        metrics = self.agent.metrics.get_summary()
        
        width = 60
        
        print(f"\n{C.NVIDIA_GREEN}╔{'═' * width}╗{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET} {C.BOLD}{C.BRIGHT_WHITE}📊 ESTADO DEL AGENTE{C.RESET}{' ' * (width - 22)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}╠{'═' * width}╣{C.RESET}")
        
        model_line = f" 🤖 Modelo: {C.BRIGHT_CYAN}{self.agent.current_model.name}{C.RESET}"
        print(f"{C.NVIDIA_GREEN}║{C.RESET}{model_line}{' ' * 25}{C.NVIDIA_GREEN}║{C.RESET}")
        
        spec_line = f"    {C.DIM}{self.agent.current_model.specialty}{C.RESET}"
        print(f"{C.NVIDIA_GREEN}║{C.RESET}{spec_line}{' ' * (width - len(self.agent.current_model.specialty) - 5)}{C.NVIDIA_GREEN}║{C.RESET}")
        
        state_line = f" 🔄 Estado: {C.BRIGHT_GREEN}{self.agent.state_machine.current_state.name}{C.RESET}"
        print(f"{C.NVIDIA_GREEN}║{C.RESET}{state_line}{' ' * 30}{C.NVIDIA_GREEN}║{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}╟{'─' * width}╢{C.RESET}")
        
        heavy_status = f"{C.BRIGHT_MAGENTA}🔥 Sí{C.RESET}" if self.agent.heavy_mode else f"{C.DIM}⚡ No{C.RESET}"
        auto_status = f"{C.BRIGHT_GREEN}🤖 Sí{C.RESET}" if self.agent.auto_mode else f"{C.DIM}👤 No{C.RESET}"
        stream_status = f"{C.BRIGHT_CYAN}📡 Sí{C.RESET}" if self.agent.stream else f"{C.DIM}📡 No{C.RESET}"
        
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Heavy Mode: {heavy_status}{' ' * 32}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Auto Mode:  {auto_status}{' ' * 32}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Streaming:  {stream_status}{' ' * 32}{C.NVIDIA_GREEN}║{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}╟{'─' * width}╢{C.RESET}")
        
        total = stats.get('total_messages', 0)
        user = stats.get('user_messages', 0)
        assistant = stats.get('assistant_messages', 0)
        
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  💬 Mensajes: {total} (👤 {user} | 🤖 {assistant}){' ' * 20}{C.NVIDIA_GREEN}║{C.RESET}")
        
        dir_str = str(self.agent.working_directory)[:35]
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  📂 Directorio: {C.DIM}{dir_str}{C.RESET}{' ' * (width - len(dir_str) - 18)}{C.NVIDIA_GREEN}║{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}╟{'─' * width}╢{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  💾 Caché: {cache_stats['size']}/{cache_stats['max_size']} | Hit rate: {cache_stats['hit_rate']}{' ' * 15}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  ⏱  Rate: {rate_stats['remaining']}/{rate_stats['max_calls']} restantes | {rate_stats['usage_percent']:.0f}% usado{' ' * 10}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  🔌 Plugins: {len(self.agent.plugin_loader.loaded_plugins)} cargados{' ' * 30}{C.NVIDIA_GREEN}║{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}╟{'─' * width}╢{C.RESET}")
        
        duration = metrics['session_duration']
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  ⏰ Sesión: {duration}{' ' * (width - len(str(duration)) - 14)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  📈 Requests: {metrics['total_requests']} | Success: {metrics['success_rate']}{' ' * 15}{C.NVIDIA_GREEN}║{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}╚{'═' * width}╝{C.RESET}\n")
        
        return CommandResult(success=True)
    
    def _cmd_cd(self, args: str) -> CommandResult:
        try:
            path = Path(args or str(Path.home())).expanduser().resolve()
            
            if path.is_dir():
                self.agent.working_directory = path
                os.chdir(path)
                self.agent.system_prompt = self.agent._build_system_prompt()
                
                print(f"\n{C.BRIGHT_GREEN}📂 Directorio cambiado:{C.RESET}")
                print(f"   {C.BRIGHT_WHITE}{path}{C.RESET}\n")
                
                self.agent.logger.debug(f"Changed directory to {path}")
                
                return CommandResult(success=True)
            else:
                return CommandResult(success=False, message=f"No es un directorio: {args}")
                
        except Exception as e:
            return CommandResult(success=False, message=f"Error: {e}")
    
    def _cmd_pwd(self, args: str) -> CommandResult:
        print(f"\n{C.BRIGHT_GREEN}📂 Directorio actual:{C.RESET}")
        print(f"   {C.BRIGHT_WHITE}{self.agent.working_directory}{C.RESET}\n")
        return CommandResult(success=True)
    
    def _cmd_ls(self, args: str) -> CommandResult:
        path = Path(args) if args else self.agent.working_directory
        
        try:
            items = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            
            print(f"\n{C.NVIDIA_GREEN}📂 {path}{C.RESET}\n")
            
            dirs = []
            files = []
            
            for item in items[:50]:
                if item.is_dir():
                    dirs.append(f"  {C.BRIGHT_BLUE}📁 {item.name}/{C.RESET}")
                else:
                    size = item.stat().st_size
                    size_str = self._format_size(size)
                    files.append(f"  {C.DIM}📄 {item.name} ({size_str}){C.RESET}")
            
            for d in dirs:
                print(d)
            for f in files:
                print(f)
            
            if len(items) > 50:
                print(f"\n  {C.DIM}... y {len(items) - 50} más{C.RESET}")
            
            print()
            return CommandResult(success=True)
            
        except PermissionError:
            return CommandResult(success=False, message="Permiso denegado")
        except Exception as e:
            return CommandResult(success=False, message=str(e))
    
    def _format_size(self, size: int) -> str:
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f}{unit}" if unit != 'B' else f"{size}{unit}"
            size /= 1024
        return f"{size:.1f}TB"
    
    def _cmd_env(self, args: str) -> CommandResult:
        if args:
            value = os.environ.get(args.upper())
            if value:
                print(f"\n{C.BRIGHT_CYAN}{args.upper()}{C.RESET} = {C.DIM}{value[:100]}{'...' if len(value) > 100 else ''}{C.RESET}\n")
            else:
                print(f"\n{C.DIM}Variable no encontrada: {args}{C.RESET}\n")
            return CommandResult(success=True)
        
        important_vars = ['PATH', 'HOME', 'USER', 'SHELL', 'NVIDIA_API_KEY', 'OPENAI_API_KEY', 
                         'PYTHON', 'VIRTUAL_ENV', 'CONDA_DEFAULT_ENV']
        
        print(f"\n{C.NVIDIA_GREEN}🔧 Variables de entorno:{C.RESET}\n")
        
        for var in important_vars:
            value = os.environ.get(var)
            if value:
                display_value = value[:50] + '...' if len(value) > 50 else value
                if 'KEY' in var or 'TOKEN' in var or 'SECRET' in var:
                    display_value = value[:4] + '***' + value[-4:] if len(value) > 8 else '***'
                print(f"  {C.BRIGHT_CYAN}{var:20}{C.RESET} = {C.DIM}{display_value}{C.RESET}")
        
        print(f"\n  {C.DIM}Usa /env <variable> para ver una específica{C.RESET}\n")
        return CommandResult(success=True)
    
    def _cmd_tools(self, args: str) -> CommandResult:
        builtin_tools = ToolRegistry.list_names()
        plugin_tools = self.agent.plugin_loader.list_plugins()
        
        total = len(builtin_tools) + len(plugin_tools)
        
        width = 55
        print(f"\n{C.NVIDIA_GREEN}╔{'═' * width}╗{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET} {C.BOLD}{C.BRIGHT_WHITE}🔧 HERRAMIENTAS ({total}){C.RESET}{' ' * (width - 22 - len(str(total)))}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}╠{'═' * width}╣{C.RESET}")
        
        if builtin_tools:
            print(f"{C.NVIDIA_GREEN}║{C.RESET}  {C.BRIGHT_YELLOW}▸ Integradas ({len(builtin_tools)}){C.RESET}{' ' * (width - 20 - len(str(len(builtin_tools))))}{C.NVIDIA_GREEN}║{C.RESET}")
            
            for name in sorted(builtin_tools)[:15]:
                tool = ToolRegistry.get(name)
                desc = ""
                if tool and hasattr(tool, 'description'):
                    desc = tool.description[:25]
                
                line = f"    {C.BRIGHT_CYAN}{name:18}{C.RESET} {C.DIM}{desc}{C.RESET}"
                print(f"{C.NVIDIA_GREEN}║{C.RESET}{line}{' ' * 5}{C.NVIDIA_GREEN}║{C.RESET}")
            
            if len(builtin_tools) > 15:
                print(f"{C.NVIDIA_GREEN}║{C.RESET}    {C.DIM}... y {len(builtin_tools) - 15} más{C.RESET}{' ' * 30}{C.NVIDIA_GREEN}║{C.RESET}")
        
        if plugin_tools:
            print(f"{C.NVIDIA_GREEN}║{C.RESET}{' ' * width}{C.NVIDIA_GREEN}║{C.RESET}")
            print(f"{C.NVIDIA_GREEN}║{C.RESET}  {C.BRIGHT_MAGENTA}▸ Plugins ({len(plugin_tools)}){C.RESET}{' ' * (width - 17 - len(str(len(plugin_tools))))}{C.NVIDIA_GREEN}║{C.RESET}")
            
            for name in sorted(plugin_tools):
                plugin = self.agent.plugin_loader.get_plugin(name)
                desc = plugin.description[:25] if plugin else ""
                
                line = f"    {C.BRIGHT_CYAN}{name:18}{C.RESET} {C.DIM}{desc}{C.RESET}"
                print(f"{C.NVIDIA_GREEN}║{C.RESET}{line}{' ' * 5}{C.NVIDIA_GREEN}║{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}╠{'═' * width}╣{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  {C.DIM}Usa /tool <nombre> para más info{C.RESET}{' ' * 18}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}╚{'═' * width}╝{C.RESET}\n")
        
        return CommandResult(success=True)
    
    def _cmd_tool_info(self, args: str) -> CommandResult:
        if not args:
            return CommandResult(success=False, message="Especifica una herramienta: /tool <nombre>")
        
        tool_name = args.strip()
        
        tool = ToolRegistry.get(tool_name)
        plugin = self.agent.plugin_loader.get_plugin(tool_name)
        
        if not tool and not plugin:
            return CommandResult(success=False, message=f"Herramienta no encontrada: {tool_name}")
        
        print(f"\n{C.NVIDIA_GREEN}═══ {tool_name} ═══{C.RESET}")
        
        if plugin:
            print(f"{C.DIM}Tipo:{C.RESET} Plugin")
            print(f"{C.DIM}Descripción:{C.RESET} {plugin.description}")
            
            if plugin.parameters:
                print(f"\n{C.BRIGHT_CYAN}Parámetros:{C.RESET}")
                for param_name, param_info in plugin.parameters.items():
                    required = "✓" if param_name in plugin.required_params else "○"
                    param_type = param_info.get('type', 'any')
                    param_desc = param_info.get('description', '')
                    print(f"  {required} {C.BRIGHT_WHITE}{param_name}{C.RESET}: {param_type} - {param_desc}")
        elif tool:
            print(f"{C.DIM}Tipo:{C.RESET} Integrada")
            if hasattr(tool, 'description'):
                print(f"{C.DIM}Descripción:{C.RESET} {tool.description}")
            
            if hasattr(tool, 'parameters'):
                print(f"\n{C.BRIGHT_CYAN}Parámetros:{C.RESET}")
                for param_name, param_info in tool.parameters.items():
                    required = "✓" if param_info.required else "○"
                    print(f"  {required} {C.BRIGHT_WHITE}{param_name}{C.RESET}: {param_info.type} - {param_info.description}")
        
        avg_time = self.agent.metrics.get_tool_avg_time(tool_name)
        usage = self.agent.metrics.tool_usage.get(tool_name, 0)
        
        if usage > 0:
            print(f"\n{C.DIM}Estadísticas:{C.RESET}")
            print(f"  • Usos en sesión: {usage}")
            print(f"  • Tiempo promedio: {avg_time:.3f}s")
        
        print()
        return CommandResult(success=True)
    
    def _cmd_plugins(self, args: str) -> CommandResult:
        parts = args.split()
        
        if not parts:
            stats = self.agent.plugin_loader.get_stats()
            errors = self.agent.plugin_loader.get_errors()
            
            print(f"\n{C.NVIDIA_GREEN}🔌 PLUGINS{C.RESET}")
            print(f"\n  {C.BRIGHT_CYAN}Cargados ({stats['loaded']}):{C.RESET}")
            
            for name in stats['plugins']:
                plugin = self.agent.plugin_loader.get_plugin(name)
                load_time = self.agent.plugin_loader.load_times.get(name, 0)
                print(f"    ✓ {C.BRIGHT_WHITE}{name}{C.RESET}: {plugin.description[:40]} {C.DIM}({load_time:.3f}s){C.RESET}")
            
            if errors:
                print(f"\n  {C.BRIGHT_RED}Errores ({len(errors)}):{C.RESET}")
                for name, error in errors.items():
                    print(f"    ✗ {C.BRIGHT_WHITE}{name}{C.RESET}: {C.DIM}{error[:50]}{C.RESET}")
            
            available = self.agent.plugin_loader.discover_plugins()
            not_loaded = [p for p in available if p not in [pl.replace(' ', '_') for pl in stats['plugins']]]
            
            if not_loaded:
                print(f"\n  {C.DIM}Disponibles no cargados: {', '.join(not_loaded)}{C.RESET}")
            
            print(f"\n  {C.DIM}Comandos: /plugins [reload|discover|load <name>]{C.RESET}\n")
            return CommandResult(success=True)
        
        cmd = parts[0].lower()
        
        if cmd == "discover":
            available = self.agent.plugin_loader.discover_plugins()
            print(f"\n{C.NVIDIA_GREEN}🔍 Plugins disponibles:{C.RESET}")
            for p in available:
                loaded = "✓" if p in self.agent.plugin_loader.loaded_plugins else "○"
                print(f"  {loaded} {p}")
            print()
        
        elif cmd == "reload":
            count = self.agent.plugin_loader.load_all()
            print(f"\n{C.BRIGHT_GREEN}✅ Plugins recargados: {count}{C.RESET}\n")
        
        elif cmd == "load" and len(parts) > 1:
            plugin_name = parts[1]
            if self.agent.plugin_loader.load_plugin(plugin_name):
                print(f"\n{C.BRIGHT_GREEN}✅ Plugin cargado: {plugin_name}{C.RESET}\n")
            else:
                error = self.agent.plugin_loader.plugin_errors.get(plugin_name, "Error desconocido")
                return CommandResult(success=False, message=f"Error cargando plugin: {error}")
        
        else:
            return CommandResult(success=False, message="Comando no válido")
        
        return CommandResult(success=True)
    
    def _cmd_plugin_reload(self, args: str) -> CommandResult:
        if not args:
            return CommandResult(success=False, message="Especifica el plugin: /plugin reload <nombre>")
        
        if self.agent.plugin_loader.reload_plugin(args):
            print(f"\n{C.BRIGHT_GREEN}✅ Plugin recargado: {args}{C.RESET}\n")
            return CommandResult(success=True)
        
        error = self.agent.plugin_loader.plugin_errors.get(args, "Error desconocido")
        return CommandResult(success=False, message=f"Error recargando: {error}")
    
    def _cmd_plugin_info(self, args: str) -> CommandResult:
        return self._cmd_tool_info(args)
    
    def _cmd_cache(self, args: str) -> CommandResult:
        stats = self.agent.response_cache.get_stats()
        
        width = 50
        print(f"\n{C.NVIDIA_GREEN}╔{'═' * width}╗{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET} {C.BOLD}{C.BRIGHT_WHITE}💾 CACHÉ DE RESPUESTAS{C.RESET}{' ' * (width - 24)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}╠{'═' * width}╣{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Entradas: {stats['size']}/{stats['max_size']}{' ' * (width - 20)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Estrategia: {stats['strategy']}{' ' * (width - len(stats['strategy']) - 16)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  TTL: {stats['ttl_minutes']} minutos{' ' * (width - 20)}{C.NVIDIA_GREEN}║{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}╟{'─' * width}╢{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Hits: {stats['hits']}{' ' * (width - len(str(stats['hits'])) - 10)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Misses: {stats['misses']}{' ' * (width - len(str(stats['misses'])) - 12)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Hit Rate: {stats['hit_rate']}{' ' * (width - len(stats['hit_rate']) - 14)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Evictions: {stats['evictions']}{' ' * (width - len(str(stats['evictions'])) - 14)}{C.NVIDIA_GREEN}║{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}╠{'═' * width}╣{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  {C.DIM}/cache clear - Limpiar caché{C.RESET}{' ' * 17}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}╚{'═' * width}╝{C.RESET}\n")
        
        return CommandResult(success=True)
    
    def _cmd_cache_clear(self, args: str) -> CommandResult:
        self.agent.response_cache.clear()
        print(f"\n{C.BRIGHT_GREEN}✅ Caché limpiado{C.RESET}\n")
        self.agent.logger.info("Cache cleared")
        return CommandResult(success=True)
    
    def _cmd_cache_stats(self, args: str) -> CommandResult:
        return self._cmd_cache("")
    
    def _cmd_ratelimit(self, args: str) -> CommandResult:
        stats = self.agent.rate_limiter.get_stats()
        info = self.agent.rate_limiter.get_info()
        
        print(f"\n{C.NVIDIA_GREEN}⏱ RATE LIMITING{C.RESET}\n")
        
        usage_bar_width = 30
        usage_percent = stats['usage_percent']
        filled = int(usage_bar_width * usage_percent / 100)
        
        if usage_percent < 50:
            bar_color = C.BRIGHT_GREEN
        elif usage_percent < 80:
            bar_color = C.BRIGHT_YELLOW
        else:
            bar_color = C.BRIGHT_RED
        
        bar = f"{bar_color}{'█' * filled}{C.DIM}{'░' * (usage_bar_width - filled)}{C.RESET}"
        
        print(f"  Uso: [{bar}] {usage_percent:.0f}%")
        print(f"  Llamadas: {stats['calls_in_window']}/{stats['max_calls']} en ventana de {stats['window_seconds']}s")
        print(f"  Restantes: {stats['remaining']}")
        
        if info.reset_time > datetime.now():
            reset_in = (info.reset_time - datetime.now()).seconds
            print(f"  Reset en: {reset_in}s")
        
        if stats['total_waits'] > 0:
            print(f"\n  {C.BRIGHT_YELLOW}Esperas totales: {stats['total_waits']}{C.RESET}")
            print(f"  {C.DIM}Tiempo total esperado: {stats['total_wait_time']}{C.RESET}")
            print(f"  {C.DIM}Espera promedio: {stats['avg_wait_time']}{C.RESET}")
        
        print()
        return CommandResult(success=True)
    
    def _cmd_ratelimit_reset(self, args: str) -> CommandResult:
        self.agent.rate_limiter.reset()
        print(f"\n{C.BRIGHT_GREEN}✅ Rate limiter reseteado{C.RESET}\n")
        return CommandResult(success=True)
    
    def _cmd_themes(self, args: str) -> CommandResult:
        if not HAS_THEMES:
            print(f"\n{C.YELLOW}⚠️  Sistema de temas no disponible{C.RESET}")
            print(f"   {C.DIM}Asegúrate de tener ui/themes.py{C.RESET}\n")
            return CommandResult(success=True)
        
        if not args:
            print(f"\n{C.NVIDIA_GREEN}🎨 TEMAS DISPONIBLES{C.RESET}\n")
            
            themes = list_themes()
            tm = get_theme_manager()
            
            for name, theme in themes.items():
                color = tm.rgb_to_ansi(theme.primary)
                marker = f" {C.GREEN}◄ actual{C.RESET}" if name == tm.current_theme_name else ""
                print(f"  {color}■{C.RESET} {name:12} {C.DIM}{theme.description}{C.RESET}{marker}")
            
            print(f"\n{C.DIM}Uso: /themes <nombre>{C.RESET}\n")
            return CommandResult(success=True)
        
        theme_name = args.strip().lower()
        if set_theme(theme_name):
            os.system('cls' if os.name == 'nt' else 'clear')
            print(f"{C.BRIGHT_GREEN}✅ Tema cambiado: {theme_name}{C.RESET}\n")
            return CommandResult(success=True)
        
        return CommandResult(success=False, message=f"Tema no encontrado: {theme_name}")
    
    def _cmd_logo(self, args: str) -> CommandResult:
        styles = ['default', 'eye', 'minimal', 'heavy', 'cyber']
        
        if not args:
            print(f"\n{C.NVIDIA_GREEN}🖼️  ESTILOS DE LOGO{C.RESET}\n")
            for s in styles:
                print(f"  • {s}")
            print(f"\n{C.DIM}Uso: /logo <estilo>{C.RESET}\n")
            return CommandResult(success=True)
        
        style = args.strip().lower()
        if style in styles:
            os.system('cls' if os.name == 'nt' else 'clear')
            print_logo(style)
            return CommandResult(success=True)
        
        return CommandResult(success=False, message=f"Estilo no válido: {style}")
    
    def _cmd_colors(self, args: str) -> CommandResult:
        print(f"\n{C.NVIDIA_GREEN}🎨 PALETA DE COLORES{C.RESET}\n")
        
        colors = [
            ('NVIDIA Green', C.NVIDIA_GREEN),
            ('Bright Green', C.BRIGHT_GREEN),
            ('Bright Cyan', C.BRIGHT_CYAN),
            ('Bright Yellow', C.BRIGHT_YELLOW),
            ('Bright Magenta', C.BRIGHT_MAGENTA),
            ('Bright Red', C.BRIGHT_RED),
            ('Bright White', C.BRIGHT_WHITE),
            ('Dim', C.DIM),
        ]
        
        for name, color in colors:
            print(f"  {color}████{C.RESET} {name}")
        
        print()
        return CommandResult(success=True)
    
    def _cmd_metrics(self, args: str) -> CommandResult:
        summary = self.agent.metrics.get_summary()
        
        width = 55
        print(f"\n{C.NVIDIA_GREEN}╔{'═' * width}╗{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET} {C.BOLD}{C.BRIGHT_WHITE}📊 MÉTRICAS DEL AGENTE{C.RESET}{' ' * (width - 24)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}╠{'═' * width}╣{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Sesión: {summary['session_duration']}{' ' * (width - len(summary['session_duration']) - 12)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Total requests: {summary['total_requests']}{' ' * (width - len(str(summary['total_requests'])) - 20)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Success rate: {summary['success_rate']}{' ' * (width - len(summary['success_rate']) - 18)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Avg time: {summary['avg_execution_time']}{' ' * (width - len(summary['avg_execution_time']) - 14)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Total tokens: {summary['total_tokens']}{' ' * (width - len(str(summary['total_tokens'])) - 18)}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  Cache hit rate: {summary['cache_hit_rate']}{' ' * (width - len(summary['cache_hit_rate']) - 20)}{C.NVIDIA_GREEN}║{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}╟{'─' * width}╢{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  {C.BRIGHT_CYAN}Top Herramientas:{C.RESET}{' ' * (width - 21)}{C.NVIDIA_GREEN}║{C.RESET}")
        for tool, count in summary['top_tools'][:5]:
            avg_time = self.agent.metrics.get_tool_avg_time(tool)
            line = f"    • {tool}: {count} usos ({avg_time:.3f}s avg)"
            print(f"{C.NVIDIA_GREEN}║{C.RESET}{line}{' ' * (width - len(line) - 1)}{C.NVIDIA_GREEN}║{C.RESET}")
        
        if summary['top_errors']:
            print(f"{C.NVIDIA_GREEN}╟{'─' * width}╢{C.RESET}")
            print(f"{C.NVIDIA_GREEN}║{C.RESET}  {C.BRIGHT_RED}Errores frecuentes:{C.RESET}{' ' * (width - 23)}{C.NVIDIA_GREEN}║{C.RESET}")
            for error, count in summary['top_errors']:
                line = f"    • {error}: {count}"
                print(f"{C.NVIDIA_GREEN}║{C.RESET}{line}{' ' * (width - len(line) - 1)}{C.NVIDIA_GREEN}║{C.RESET}")
        
        print(f"{C.NVIDIA_GREEN}╠{'═' * width}╣{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  {C.DIM}/metrics export - Exportar a JSON{C.RESET}{' ' * 16}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}║{C.RESET}  {C.DIM}/metrics reset - Resetear métricas{C.RESET}{' ' * 15}{C.NVIDIA_GREEN}║{C.RESET}")
        print(f"{C.NVIDIA_GREEN}╚{'═' * width}╝{C.RESET}\n")
        
        return CommandResult(success=True)
    
    def _cmd_metrics_export(self, args: str) -> CommandResult:
        METRICS_EXPORT_DIR.mkdir(exist_ok=True)
        filename = args.strip() or f"metrics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        if not filename.endswith('.json'):
            filename += '.json'
        
        filepath = METRICS_EXPORT_DIR / filename
        self.agent.metrics.export_to_json(filepath)
        
        print(f"\n{C.BRIGHT_GREEN}✅ Métricas exportadas a: {filepath}{C.RESET}\n")
        self.agent.logger.info(f"Metrics exported to {filepath}")
        
        return CommandResult(success=True)
    
    def _cmd_metrics_reset(self, args: str) -> CommandResult:
        self.agent.metrics.reset()
        print(f"\n{C.BRIGHT_GREEN}✅ Métricas reseteadas{C.RESET}\n")
        self.agent.logger.info("Metrics reset")
        return CommandResult(success=True)
    
    def _cmd_log(self, args: str) -> CommandResult:
        if not args:
            current_level = logging.getLevelName(self.agent.logger.logger.level)
            
            print(f"\n{C.NVIDIA_GREEN}📝 LOGGING{C.RESET}\n")
            print(f"  Nivel actual: {C.BRIGHT_CYAN}{current_level}{C.RESET}")
            print(f"  Directorio: {LOG_DIR}")
            
            log_files = list(LOG_DIR.glob("*.log"))
            if log_files:
                print(f"\n  Archivos de log:")
                for lf in log_files[:5]:
                    size = lf.stat().st_size
                    size_str = self._format_size(size)
                    print(f"    • {lf.name} ({size_str})")
            
            print(f"\n  {C.DIM}Niveles: DEBUG, INFO, WARNING, ERROR, CRITICAL{C.RESET}")
            print(f"  {C.DIM}Uso: /log level <nivel>{C.RESET}\n")
            return CommandResult(success=True)
        
        return self._cmd_log_level(args)
    
    def _cmd_log_level(self, args: str) -> CommandResult:
        if not args:
            return CommandResult(success=False, message="Especifica el nivel: DEBUG, INFO, WARNING, ERROR")
        
        level = args.upper()
        if level in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
            self.agent.logger.set_console_level(level)
            print(f"\n{C.BRIGHT_GREEN}✅ Nivel de log cambiado a: {level}{C.RESET}\n")
            self.agent.logger.info(f"Log level changed to {level}")
            return CommandResult(success=True)
        
        return CommandResult(success=False, message="Nivel inválido")
    
    def _cmd_debug(self, args: str) -> CommandResult:
        if args == "on":
            self.agent.logger.set_console_level("DEBUG")
            print(f"\n{C.BRIGHT_MAGENTA}🔧 Modo debug ACTIVADO{C.RESET}\n")
        elif args == "off":
            self.agent.logger.set_console_level("INFO")
            print(f"\n{C.DIM}🔧 Modo debug DESACTIVADO{C.RESET}\n")
        else:
            current = logging.getLevelName(self.agent.logger.console_handler.level)
            is_debug = current == "DEBUG"
            
            print(f"\n{C.NVIDIA_GREEN}🔧 DEBUG MODE{C.RESET}")
            print(f"  Estado: {'Activado' if is_debug else 'Desactivado'}")
            print(f"  Nivel de log: {current}")
            print(f"\n  {C.DIM}Uso: /debug [on|off]{C.RESET}\n")
        
        return CommandResult(success=True)
    
    def _cmd_stats(self, args: str) -> CommandResult:
        stats = self.agent.conversation.get_stats()
        cache_stats = self.agent.response_cache.get_stats()
        rate_stats = self.agent.rate_limiter.get_stats()
        plugin_stats = self.agent.plugin_loader.get_stats()
        metrics = self.agent.metrics.get_summary()
        
        print(f"\n{C.NVIDIA_GREEN}═══ ESTADÍSTICAS DETALLADAS ═══{C.RESET}\n")
        
        print(f"  {C.BRIGHT_CYAN}Conversación:{C.RESET}")
        print(f"    • Total mensajes: {stats.get('total_messages', 0)}")
        print(f"    • Mensajes usuario: {stats.get('user_messages', 0)}")
        print(f"    • Mensajes asistente: {stats.get('assistant_messages', 0)}")
        print(f"    • Llamadas a herramientas: {stats.get('tool_calls', 0)}")
        
        print(f"\n  {C.BRIGHT_CYAN}Sesión:{C.RESET}")
        print(f"    • Duración: {metrics['session_duration']}")
        print(f"    • Modelo: {self.agent.current_model.name}")
        print(f"    • Directorio: {self.agent.working_directory}")
        print(f"    • Heavy Mode: {'Sí' if self.agent.heavy_mode else 'No'}")
        print(f"    • Auto Mode: {'Sí' if self.agent.auto_mode else 'No'}")
        
        print(f"\n  {C.BRIGHT_CYAN}Rendimiento:{C.RESET}")
        print(f"    • Total requests: {metrics['total_requests']}")
        print(f"    • Success rate: {metrics['success_rate']}")
        print(f"    • Avg response time: {metrics['avg_execution_time']}")
        print(f"    • Total tokens: {metrics['total_tokens']}")
        
        print(f"\n  {C.BRIGHT_CYAN}Caché:{C.RESET}")
        print(f"    • Entradas: {cache_stats['size']}/{cache_stats['max_size']}")
        print(f"    • Hit rate: {cache_stats['hit_rate']}")
        print(f"    • Hits/Misses: {cache_stats['hits']}/{cache_stats['misses']}")
        print(f"    • Evictions: {cache_stats['evictions']}")
        
        print(f"\n  {C.BRIGHT_CYAN}Rate Limiting:{C.RESET}")
        print(f"    • Uso actual: {rate_stats['usage_percent']:.1f}%")
        print(f"    • Llamadas restantes: {rate_stats['remaining']}")
        print(f"    • Esperas totales: {rate_stats['total_waits']}")
        
        print(f"\n  {C.BRIGHT_CYAN}Plugins:{C.RESET}")
        print(f"    • Cargados: {plugin_stats['loaded']}")
        print(f"    • Errores: {plugin_stats['errors']}")
        
        print(f"\n  {C.BRIGHT_CYAN}Estado:{C.RESET}")
        print(f"    • Estado actual: {self.agent.state_machine.current_state.name}")
        print(f"    • Transiciones: {len(self.agent.state_machine.state_history)}")
        
        print()
        return CommandResult(success=True)
    
    def _cmd_state(self, args: str) -> CommandResult:
        sm = self.agent.state_machine
        
        print(f"\n{C.NVIDIA_GREEN}🔄 MÁQUINA DE ESTADOS{C.RESET}\n")
        
        print(f"  Estado actual: {C.BRIGHT_GREEN}{sm.current_state.name}{C.RESET}")
        if sm.previous_state:
            print(f"  Estado anterior: {C.DIM}{sm.previous_state.name}{C.RESET}")
        
        print(f"\n  {C.BRIGHT_CYAN}Transiciones válidas desde {sm.current_state.name}:{C.RESET}")
        valid = sm.valid_transitions.get(sm.current_state, set())
        for state in valid:
            print(f"    → {state.name}")
        
        print(f"\n  {C.BRIGHT_CYAN}Historial reciente:{C.RESET}")
        history = sm.get_history(10)
        if history:
            for transition in history[-5:]:
                timestamp = transition.timestamp.strftime('%H:%M:%S')
                reason = f" ({transition.reason})" if transition.reason else ""
                print(f"    [{timestamp}] {transition.from_state.name} → {transition.to_state.name}{C.DIM}{reason}{C.RESET}")
        else:
            print(f"    {C.DIM}Sin historial{C.RESET}")
        
        print()
        return CommandResult(success=True)


class NVIDIACodeAgent:
    def __init__(
        self,
        initial_model: str = "1",
        working_directory: str = None,
        stream: bool = True,
        heavy_mode: bool = False,
        auto_mode: bool = False,
        log_level: str = "INFO",
        cache_enabled: bool = True,
        plugins_enabled: bool = True
    ):
        self.metrics = AgentMetrics()
        
        self.logger = AgentLogger(
            name="nvidia_code",
            log_dir=LOG_DIR,
            console_level=log_level
        )
        
        self.state_machine = StateMachine(metrics=self.metrics)
        
        self.api_client = NVIDIAAPIClient()
        self.registry = ModelRegistry()
        self.conversation = ConversationManager()
        self.heavy_agent = HeavyAgent(self.api_client)
        self.tool_display = ToolDisplayManager()
        
        self.response_cache = ResponseCache(
            ttl_minutes=CACHE_TTL_MINUTES,
            max_size=CACHE_MAX_SIZE,
            strategy=CacheStrategy.HYBRID
        ) if cache_enabled else None
        
        self.rate_limiter = RateLimiter(
            max_calls=RATE_LIMIT_CALLS,
            window_seconds=RATE_LIMIT_WINDOW
        )
        
        self.plugin_loader = PluginLoader(PLUGIN_DIR)
        if plugins_enabled:
            loaded = self.plugin_loader.load_all()
            if loaded > 0:
                self.logger.info(f"Loaded {loaded} plugins")
        
        self.tool_executor = ToolExecutor(
            display=self.tool_display,
            metrics=self.metrics,
            logger=self.logger
        )
        
        self.autosave_manager = AutoSaveManager()
        
        self.current_model = self.registry.get(initial_model) or self.registry.get("1")
        self.working_directory = Path(working_directory or os.getcwd()).resolve()
        self.stream = stream
        self.heavy_mode = heavy_mode
        self.auto_mode = auto_mode
        
        self._session_start = datetime.now()
        self._shutdown_handlers_registered = False
        
        self.command_handler = CommandHandler(self)
        self.input_handler = InputHandler(self.command_handler)
        
        os.chdir(self.working_directory)
        
        self.system_prompt = self._build_system_prompt()
        
        self._register_shutdown_handlers()
        
        self.logger.info(f"Agent initialized with model {self.current_model.name}")
    
    def _register_shutdown_handlers(self):
        if self._shutdown_handlers_registered:
            return
        
        def cleanup():
            self.logger.info("Agent shutting down")
            self.input_handler.save_history()
            
            if len(self.conversation) > 0:
                self.autosave_manager.save(
                    [asdict(m) for m in self.conversation.messages],
                    self.current_model.id,
                    {'shutdown': True}
                )
            
            METRICS_EXPORT_DIR.mkdir(exist_ok=True)
            self.metrics.export_to_json(
                METRICS_EXPORT_DIR / f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            )
        
        atexit.register(cleanup)
        
        def signal_handler(signum, frame):
            print(f"\n{C.YELLOW}Señal recibida, limpiando...{C.RESET}")
            cleanup()
            sys.exit(0)
        
        try:
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
        except (ValueError, OSError):
            pass
        
        self._shutdown_handlers_registered = True
    
    def _build_system_prompt(self) -> str:
        builtin_tools = ToolRegistry.list_names()[:15]
        plugin_tools = self.plugin_loader.list_plugins()[:10]
        all_tools = builtin_tools + plugin_tools
        tools_list = ", ".join(sorted(all_tools))
        
        return f"""Eres NVIDIA Code, un agente de programación experto y asistente técnico avanzado.

═══════════════════════════════════════════════════════════════════════════════
INFORMACIÓN DEL SISTEMA
═══════════════════════════════════════════════════════════════════════════════
• Fecha y hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
• Directorio de trabajo: {self.working_directory}
• Sistema operativo: {sys.platform}
• Python: {sys.version.split()[0]}
• Modelo actual: {self.current_model.name}
• Herramientas disponibles: {tools_list}
• Plugins cargados: {len(self.plugin_loader.loaded_plugins)}

═══════════════════════════════════════════════════════════════════════════════
REGLAS Y DIRECTIVAS
═══════════════════════════════════════════════════════════════════════════════
1. CÓDIGO COMPLETO: Siempre muestra código completo y funcional.
   - NUNCA uses "..." o "// resto del código"
   - NUNCA omitas partes importantes
   - Si el código es muy largo, divídelo en secciones claras

2. USO DE HERRAMIENTAS: Úsalas activamente para:
   - Leer/escribir archivos
   - Ejecutar comandos
   - Buscar en la web
   - Analizar el sistema

3. RESPUESTAS OBLIGATORIAS:
   - SIEMPRE analiza y explica los resultados de las herramientas
   - NUNCA te quedes sin responder después de usar una herramienta
   - Si una herramienta falla, explica el error y sugiere alternativas

4. FORMATO:
   - Responde en español
   - Usa markdown para formatear
   - Sé conciso pero completo
   - Incluye ejemplos cuando sea útil

5. SEGURIDAD:
   - Advierte sobre operaciones peligrosas
   - Pide confirmación para eliminar archivos
   - No ejecutes código malicioso

═══════════════════════════════════════════════════════════════════════════════
FLUJO DE TRABAJO
═══════════════════════════════════════════════════════════════════════════════
1. Analiza la solicitud del usuario
2. Si necesitas información: usa las herramientas apropiadas
3. Procesa los resultados de las herramientas
4. Proporciona una respuesta clara y útil
5. Sugiere siguientes pasos si es apropiado
"""
    
    def chat(self, user_input: str) -> str:
        if not self.state_machine.transition_to(AgentState.PROCESSING, "User input received"):
            self.logger.warning(f"Cannot process: agent in state {self.state_machine.current_state}")
            return f"⚠️ El agente está ocupado ({self.state_machine.current_state.name})"
        
        start_time = time.time()
        success = False
        tokens_used = 0
        final_response = ""
        
        try:
            if self.heavy_mode:
                self.state_machine.transition_to(AgentState.HEAVY_MODE, "Heavy mode enabled")
                response = self._process_heavy_mode(user_input)
                success = True
                final_response = response
                return response
            
            self.conversation.add_user_message(user_input)
            self.autosave_manager.record_message()
            
            wait_time = self.rate_limiter.wait_if_needed()
            if wait_time > 0:
                self.metrics.record_rate_limit_wait()
            
            if self.response_cache and not self.current_model.supports_tools:
                cached = self.response_cache.get(
                    self.conversation.get_api_messages(),
                    self.current_model.id
                )
                if cached:
                    self.metrics.record_cache_hit()
                    self.logger.debug("Cache hit")
                    self.conversation.add_assistant_message(cached)
                    success = True
                    final_response = cached
                    return cached
                else:
                    self.metrics.record_cache_miss()
            
            tools = None
            if self.current_model.supports_tools:
                tools = self._get_all_tools()
            
            iteration = 0
            tool_was_used = False
            last_tool_results: List[ToolExecutionResult] = []
            
            while iteration < MAX_TOOL_ITERATIONS:
                iteration += 1
                
                self.logger.debug(f"Processing iteration {iteration}")
                
                messages = self.conversation.get_api_messages(self.system_prompt)
                
                try:
                    response = self.api_client.chat(
                        messages=messages,
                        model=self.current_model,
                        tools=tools,
                        stream=self.stream
                    )
                except Exception as e:
                    self.state_machine.transition_to(AgentState.ERROR, str(e))
                    error_msg = self._format_error(e)
                    self.logger.exception("API call failed")
                    self.conversation.add_assistant_message(error_msg)
                    self.metrics.record_error(type(e).__name__)
                    return error_msg
                
                if response.content and response.content.startswith("[Error]"):
                    self.state_machine.transition_to(AgentState.ERROR, "API returned error")
                    self.conversation.add_assistant_message(response.content)
                    self.metrics.record_error("APIError")
                    return response.content
                
                if hasattr(response, 'usage') and response.usage:
                    tokens_used += response.usage.get('total_tokens', 0)
                
                if response.tool_calls:
                    tool_was_used = True
                    self.state_machine.transition_to(AgentState.WAITING_TOOLS, "Processing tool calls")
                    
                    tool_calls = response.tool_calls[:MAX_TOOLS_PER_ITERATION]
                    
                    if len(response.tool_calls) > MAX_TOOLS_PER_ITERATION:
                        print(f"{C.YELLOW}⚠️  Limitando a {MAX_TOOLS_PER_ITERATION} herramientas{C.RESET}")
                        self.logger.warning(f"Tool calls limited from {len(response.tool_calls)} to {MAX_TOOLS_PER_ITERATION}")
                    
                    self.conversation.add_assistant_message(
                        response.content or "",
                        tool_calls=tool_calls
                    )
                    
                    results = self.tool_executor.execute_batch(
                        tool_calls,
                        self.conversation,
                        self.plugin_loader
                    )
                    last_tool_results.extend(results)
                    
                    self.state_machine.transition_to(AgentState.PROCESSING, "Tools executed")
                    continue
                
                else:
                    if tool_was_used and not response.content:
                        final_response = self._handle_empty_response(last_tool_results)
                    else:
                        final_response = response.content or ""
                    
                    if final_response:
                        self.conversation.add_assistant_message(final_response)
                        self.autosave_manager.record_message()
                    
                    success = True
                    break
            
            if not final_response and tool_was_used:
                final_response = self._generate_fallback_response(last_tool_results)
                self.conversation.add_assistant_message(final_response)
            
            if self.response_cache and not tool_was_used and final_response:
                self.response_cache.set(
                    self.conversation.get_api_messages()[:-1],
                    self.current_model.id,
                    final_response,
                    tokens_used
                )
            
            if self.autosave_manager.should_save():
                filepath = self.autosave_manager.save(
                    [asdict(m) for m in self.conversation.messages],
                    self.current_model.id
                )
                if filepath:
                    print(f"{C.DIM}💾 Auto-guardado: {filepath.name}{C.RESET}")
            
            success = True
            return final_response
            
        except Exception as e:
            self.state_machine.transition_to(AgentState.ERROR, str(e))
            self.logger.exception("Unexpected error in chat")
            self.metrics.record_error(type(e).__name__)
            return f"[Error] {type(e).__name__}: {str(e)}"
            
        finally:
            elapsed = time.time() - start_time
            self.metrics.record_request(success, tokens_used, elapsed, self.current_model.id)
            self.state_machine.transition_to(AgentState.IDLE, "Processing complete")
            self.logger.debug(f"Chat completed in {elapsed:.2f}s, success={success}")
    
    def _process_heavy_mode(self, user_input: str) -> str:
        self.logger.info("Processing in heavy mode")
        
        try:
            response = self.heavy_agent.process(
                user_input,
                self.system_prompt,
                self.conversation.get_api_messages()
            )
            
            self.conversation.add_user_message(user_input)
            self.conversation.add_assistant_message(response)
            
            return response
            
        except Exception as e:
            self.logger.exception("Heavy mode processing failed")
            self.state_machine.transition_to(AgentState.ERROR, str(e))
            raise
    
    def _get_all_tools(self) -> List[Dict[str, Any]]:
        tools = ToolRegistry.to_openai_format()
        
        for name, plugin in self.plugin_loader.loaded_plugins.items():
            if hasattr(plugin, 'get_openai_schema'):
                tools.append(plugin.get_openai_schema())
            else:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": plugin.name,
                        "description": plugin.description,
                        "parameters": {
                            "type": "object",
                            "properties": getattr(plugin, 'parameters', {}),
                            "required": getattr(plugin, 'required_params', [])
                        }
                    }
                })
        
        return tools
    
    def _handle_empty_response(self, tool_results: List[ToolExecutionResult]) -> str:
        print(f"{C.DIM}(Analizando resultados...){C.RESET}")
        self.logger.debug("Handling empty response after tool use")
        
        self.conversation.add_user_message(
            "Analiza los resultados de las herramientas y proporciona tu respuesta al usuario."
        )
        
        try:
            wait_time = self.rate_limiter.wait_if_needed()
            if wait_time > 0:
                self.metrics.record_rate_limit_wait()
            
            continuation = self.api_client.chat(
                messages=self.conversation.get_api_messages(self.system_prompt),
                model=self.current_model,
                tools=None,
                stream=self.stream
            )
            
            if continuation.content and not continuation.content.startswith("[Error]"):
                return continuation.content
            else:
                return self._generate_fallback_response(tool_results)
                
        except Exception as e:
            self.logger.error(f"Error getting continuation: {e}")
            return self._generate_fallback_response(tool_results)
    
    def _generate_fallback_response(self, tool_results: List[ToolExecutionResult]) -> str:
        if not tool_results:
            return "He procesado tu solicitud."
        
        self.logger.debug(f"Generating fallback response for {len(tool_results)} tool results")
        
        response_parts = ["📊 **Resultados obtenidos:**\n"]
        
        for result in tool_results:
            status = "✅" if result.success else "❌"
            response_parts.append(f"\n**{status} {result.tool_name}:**")
            
            if result.success:
                preview = result.result_preview
                if len(preview) > 50:
                    response_parts.append(f"```\n{preview}\n```")
                else:
                    response_parts.append(f"`{preview}`")
            else:
                response_parts.append(f"*Error: {result.error_message}*")
        
        response_parts.append("\n¿Qué te gustaría que analice o haga con esta información?")
        
        return "\n".join(response_parts)
    
    def _format_error(self, error: Exception) -> str:
        error_type = type(error).__name__
        error_msg = str(error)[:200]
        return f"[Error] {error_type}: {error_msg}"
    
    def run(self):
        os.system('cls' if os.name == 'nt' else 'clear')
        self._print_welcome()
        
        self.logger.info("Agent interactive mode started")
        
        while True:
            try:
                prompt = self._build_prompt()
                user_input = self.input_handler.get_input(prompt)
                
                if not user_input:
                    continue
                
                if user_input.lower() in ['continua', 'continúa', 'continue', 'sigue', 'c']:
                    user_input = "Continúa con tu respuesta anterior."
                
                if user_input.startswith('/'):
                    result = self.command_handler.execute(user_input)
                    if not result.success and result.message:
                        print(f"{C.RED}{result.message}{C.RESET}")
                    continue
                
                response = self.chat(user_input)
                
                if not self.stream and response and not response.startswith("[Error]"):
                    try:
                        print()
                        print_markdown(response)
                        print()
                    except Exception:
                        print(f"\n{response}\n")
                
            except KeyboardInterrupt:
                print(f"\n{C.YELLOW}Usa /exit para salir{C.RESET}")
            except EOFError:
                break
            except Exception as e:
                self.logger.exception("Error in main loop")
                print(f"{C.RED}Error inesperado: {e}{C.RESET}")
    
    def _build_prompt(self) -> str:
        prompt_color = C.NVIDIA_GREEN
        current_theme_name = ""
        if HAS_THEMES:
            try:
                tm = get_theme_manager()
                prompt_color = tm.rgb_to_ansi(tm.current_theme.primary)
                current_theme_name = tm.current_theme_name
            except:
                pass
        
        mode_indicators = []
        if self.heavy_mode:
            mode_indicators.append(f"{C.BRIGHT_MAGENTA}🔥{C.RESET}")
        if self.auto_mode:
            mode_indicators.append(f"{C.BRIGHT_GREEN}🤖{C.RESET}")
        if self.state_machine.current_state != AgentState.IDLE:
            mode_indicators.append(f"{C.BRIGHT_YELLOW}⚡{C.RESET}")
        
        mode_str = " " + " ".join(mode_indicators) if mode_indicators else ""
        
        model_display = f"{C.BOLD}{self.current_model.name}{C.RESET}"
        
        rate_info = self.rate_limiter.get_info()
        if rate_info.remaining < 10:
            rate_indicator = f" {C.BRIGHT_RED}[{rate_info.remaining}]{C.RESET}"
        elif rate_info.remaining < 30:
            rate_indicator = f" {C.BRIGHT_YELLOW}[{rate_info.remaining}]{C.RESET}"
        else:
            rate_indicator = ""
        
        if current_theme_name == "claude_code":
            line1 = f"\n{C.DIM}model{C.RESET} {model_display}{mode_str}{rate_indicator}"
            line2 = f"{prompt_color}›{C.RESET} "
        else:
            line1 = f"\n{prompt_color}┌─{C.RESET} {model_display}{mode_str}{rate_indicator} {prompt_color}─{C.RESET}"
            line2 = f"{prompt_color}└─>{C.RESET} "

        return f"{line1}\n{line2}"
    
    def _print_welcome(self):
        from ui.logo import print_welcome

        recent = []
        try:
            chats = ChatStorage.list_chats()
            for chat in chats[:5]:
                recent.append({
                    'name': chat.name,
                    'date': chat.last_modified_formatted,
                    'messages': chat.message_count,
                })
        except:
            pass

        print_welcome(
            model_name=self.current_model.name,
            model_specialty=self.current_model.specialty.replace("🧠 ", "").replace("💻 ", "").replace("⚡ ", "").replace("🌐 ", ""),
            directory=str(self.working_directory),
            heavy_mode=self.heavy_mode,
            auto_mode=self.auto_mode,
            plugins_count=len(self.plugin_loader.loaded_plugins),
            recent_chats=recent,
        )
    
    def process_file(self, filepath: str, instruction: str = None) -> str:
        path = Path(filepath)
        
        if not path.exists():
            return f"[Error] Archivo no encontrado: {filepath}"
        
        try:
            content = path.read_text(encoding='utf-8')
        except Exception as e:
            return f"[Error] No se pudo leer el archivo: {e}"
        
        prompt = f"Archivo: {path.name}\n\n```\n{content}\n```"
        
        if instruction:
            prompt += f"\n\nInstrucción: {instruction}"
        else:
            prompt += "\n\nAnaliza este archivo y proporciona información útil."
        
        return self.chat(prompt)
    
    def execute_command(self, command: str) -> str:
        prompt = f"Ejecuta el siguiente comando y analiza el resultado:\n\n```bash\n{command}\n```"
        return self.chat(prompt)
    
    def analyze_error(self, error: str, context: str = None) -> str:
        prompt = f"Analiza el siguiente error:\n\n```\n{error}\n```"
        
        if context:
            prompt += f"\n\nContexto:\n{context}"
        
        prompt += "\n\nExplica la causa y sugiere soluciones."
        
        return self.chat(prompt)
    
    def generate_code(self, specification: str, language: str = "python") -> str:
        prompt = f"""Genera código {language} basado en la siguiente especificación:

{specification}

Requisitos:
1. Código completo y funcional
2. Incluye comentarios explicativos
3. Manejo de errores apropiado
4. Sigue las mejores prácticas"""
        
        return self.chat(prompt)
    
    def review_code(self, code: str, language: str = "python") -> str:
        prompt = f"""Revisa el siguiente código {language}:

```{language}
{code}
```

Proporciona:
1. Análisis de calidad
2. Problemas potenciales
3. Sugerencias de mejora
4. Optimizaciones posibles"""
        
        return self.chat(prompt)
    
    def explain_code(self, code: str, level: str = "intermediate") -> str:
        levels = {
            "beginner": "para alguien que está aprendiendo a programar",
            "intermediate": "para un desarrollador con experiencia básica",
            "advanced": "con detalles técnicos avanzados"
        }
        
        level_desc = levels.get(level, levels["intermediate"])
        
        prompt = f"""Explica el siguiente código {level_desc}:

```
{code}
```

Incluye:
1. Propósito general
2. Explicación línea por línea
3. Conceptos importantes utilizados
4. Posibles casos de uso"""
        
        return self.chat(prompt)
    
    def get_session_info(self) -> Dict[str, Any]:
        return {
            'model': self.current_model.name,
            'model_id': self.current_model.id,
            'working_directory': str(self.working_directory),
            'heavy_mode': self.heavy_mode,
            'auto_mode': self.auto_mode,
            'stream': self.stream,
            'messages': len(self.conversation),
            'state': self.state_machine.current_state.name,
            'session_duration': str(self.metrics.get_session_duration()),
            'metrics': self.metrics.get_summary(),
            'cache_stats': self.response_cache.get_stats() if self.response_cache else None,
            'rate_limit': self.rate_limiter.get_stats(),
            'plugins': self.plugin_loader.get_stats()
        }
    
    def export_session(self, filepath: str = None) -> Path:
        if not filepath:
            filepath = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        path = Path(filepath)
        
        data = {
            'exported_at': datetime.now().isoformat(),
            'session_info': self.get_session_info(),
            'conversation': [asdict(m) for m in self.conversation.messages],
            'system_prompt': self.system_prompt
        }
        
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        
        self.logger.info(f"Session exported to {path}")
        
        return path
    
    def import_session(self, filepath: str) -> bool:
        path = Path(filepath)
        
        if not path.exists():
            self.logger.error(f"Session file not found: {filepath}")
            return False
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            messages_data = data.get('conversation', [])
            self.conversation.messages = [Message(**m) for m in messages_data]
            
            session_info = data.get('session_info', {})
            if 'heavy_mode' in session_info:
                self.heavy_mode = session_info['heavy_mode']
            if 'auto_mode' in session_info:
                self.auto_mode = session_info['auto_mode']
            
            self.logger.info(f"Session imported from {path}")
            return True
            
        except Exception as e:
            self.logger.exception(f"Error importing session: {e}")
            return False
    
    def cancel_current_operation(self):
        self.tool_executor.cancel_all()
        self.state_machine.force_state(AgentState.IDLE, "Operation cancelled by user")
        self.logger.info("Current operation cancelled")
    
    def reset(self):
        self.conversation.clear()
        self.response_cache.clear() if self.response_cache else None
        self.metrics.reset()
        self.state_machine.reset()
        self.tool_executor.reset()
        self.system_prompt = self._build_system_prompt()
        self.logger.info("Agent reset")
    
    def __len__(self) -> int:
        return len(self.conversation)
    
    def __repr__(self) -> str:
        return (f"NVIDIACodeAgent(model={self.current_model.name}, "
                f"messages={len(self)}, state={self.state_machine.current_state.name}, "
                f"heavy={self.heavy_mode})")
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.input_handler.save_history()
        
        if len(self.conversation) > 0:
            self.autosave_manager.save(
                [asdict(m) for m in self.conversation.messages],
                self.current_model.id,
                {'context_exit': True}
            )
        
        return False


def create_agent(
    model: str = "1",
    directory: str = None,
    heavy: bool = False,
    auto: bool = False,
    stream: bool = True,
    log_level: str = "INFO",
    cache: bool = True,
    plugins: bool = True
) -> NVIDIACodeAgent:
    return NVIDIACodeAgent(
        initial_model=model,
        working_directory=directory,
        heavy_mode=heavy,
        auto_mode=auto,
        stream=stream,
        log_level=log_level,
        cache_enabled=cache,
        plugins_enabled=plugins
    )


def quick_chat(message: str, model: str = "1") -> str:
    with NVIDIACodeAgent(initial_model=model, stream=False) as agent:
        return agent.chat(message)


def process_file(filepath: str, instruction: str = None, model: str = "1") -> str:
    with NVIDIACodeAgent(initial_model=model, stream=False) as agent:
        return agent.process_file(filepath, instruction)


def batch_process(messages: List[str], model: str = "1", 
                  callback: Callable[[int, str], None] = None) -> List[str]:
    results = []
    
    with NVIDIACodeAgent(initial_model=model, stream=False) as agent:
        for i, message in enumerate(messages):
            response = agent.chat(message)
            results.append(response)
            
            if callback:
                callback(i, response)
    
    return results


class AgentBuilder:
    def __init__(self):
        self._model = "1"
        self._directory = None
        self._heavy = False
        self._auto = False
        self._stream = True
        self._log_level = "INFO"
        self._cache = True
        self._plugins = True
        self._custom_tools = []
        self._system_prompt_additions = []
    
    def model(self, model_id: str) -> 'AgentBuilder':
        self._model = model_id
        return self
    
    def directory(self, path: str) -> 'AgentBuilder':
        self._directory = path
        return self
    
    def heavy_mode(self, enabled: bool = True) -> 'AgentBuilder':
        self._heavy = enabled
        return self
    
    def auto_mode(self, enabled: bool = True) -> 'AgentBuilder':
        self._auto = enabled
        return self
    
    def stream(self, enabled: bool = True) -> 'AgentBuilder':
        self._stream = enabled
        return self
    
    def log_level(self, level: str) -> 'AgentBuilder':
        self._log_level = level
        return self
    
    def cache(self, enabled: bool = True) -> 'AgentBuilder':
        self._cache = enabled
        return self
    
    def plugins(self, enabled: bool = True) -> 'AgentBuilder':
        self._plugins = enabled
        return self
    
    def add_tool(self, tool: ToolPlugin) -> 'AgentBuilder':
        self._custom_tools.append(tool)
        return self
    
    def add_system_instruction(self, instruction: str) -> 'AgentBuilder':
        self._system_prompt_additions.append(instruction)
        return self
    
    def build(self) -> NVIDIACodeAgent:
        agent = NVIDIACodeAgent(
            initial_model=self._model,
            working_directory=self._directory,
            heavy_mode=self._heavy,
            auto_mode=self._auto,
            stream=self._stream,
            log_level=self._log_level,
            cache_enabled=self._cache,
            plugins_enabled=self._plugins
        )
        
        for tool in self._custom_tools:
            agent.plugin_loader.loaded_plugins[tool.name] = tool
        
        if self._system_prompt_additions:
            additions = "\n".join(self._system_prompt_additions)
            agent.system_prompt += f"\n\nINSTRUCCIONES ADICIONALES:\n{additions}"
        
        return agent


class ConversationContext:
    def __init__(self, agent: NVIDIACodeAgent):
        self.agent = agent
        self._original_messages = []
    
    def __enter__(self):
        self._original_messages = list(self.agent.conversation.messages)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.agent.conversation.messages = self._original_messages
        return False
    
    def chat(self, message: str) -> str:
        return self.agent.chat(message)


def with_temporary_context(agent: NVIDIACodeAgent) -> ConversationContext:
    return ConversationContext(agent)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="NVIDIA Code Agent - Asistente de programación avanzado",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python agent.py                    # Iniciar en modo interactivo
  python agent.py -m 2               # Usar modelo 2
  python agent.py --heavy            # Activar Heavy Agent
  python agent.py -d /path/to/dir    # Establecer directorio de trabajo
  python agent.py --no-stream        # Desactivar streaming
  python agent.py --debug            # Modo debug
        """
    )
    
    parser.add_argument(
        "-m", "--model",
        default="1",
        help="Modelo inicial (número o nombre)"
    )
    parser.add_argument(
        "-d", "--directory",
        default=None,
        help="Directorio de trabajo inicial"
    )
    parser.add_argument(
        "--heavy",
        action="store_true",
        help="Activar Heavy Agent (modo multi-IA)"
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Activar modo automático"
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Desactivar streaming de respuestas"
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Desactivar caché de respuestas"
    )
    parser.add_argument(
        "--no-plugins",
        action="store_true",
        help="Desactivar carga de plugins"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Activar modo debug (log level DEBUG)"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Nivel de logging"
    )
    parser.add_argument(
        "-c", "--command",
        default=None,
        help="Ejecutar un comando y salir"
    )
    parser.add_argument(
        "-f", "--file",
        default=None,
        help="Procesar un archivo"
    )
    parser.add_argument(
        "-i", "--instruction",
        default=None,
        help="Instrucción para procesar archivo"
    )
    parser.add_argument(
        "--export",
        default=None,
        help="Exportar sesión a archivo al finalizar"
    )
    parser.add_argument(
        "--import-session",
        default=None,
        help="Importar sesión desde archivo"
    )
    parser.add_argument(
        "--version",
        action="version",
        version="NVIDIA Code Agent v2.0.0"
    )
    
    args = parser.parse_args()
    
    log_level = "DEBUG" if args.debug else args.log_level
    
    agent = NVIDIACodeAgent(
        initial_model=args.model,
        working_directory=args.directory,
        stream=not args.no_stream,
        heavy_mode=args.heavy,
        auto_mode=args.auto,
        log_level=log_level,
        cache_enabled=not args.no_cache,
        plugins_enabled=not args.no_plugins
    )
    
    if args.import_session:
        if not agent.import_session(args.import_session):
            print(f"{C.RED}Error importando sesión{C.RESET}")
            sys.exit(1)
    
    try:
        if args.command:
            response = agent.chat(args.command)
            try:
                print_markdown(response)
            except:
                print(response)
        
        elif args.file:
            response = agent.process_file(args.file, args.instruction)
            try:
                print_markdown(response)
            except:
                print(response)
        
        else:
            agent.run()
    
    finally:
        if args.export:
            agent.export_session(args.export)


if __name__ == "__main__":
    main()
