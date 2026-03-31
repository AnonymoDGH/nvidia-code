import os
import sys
import time
import threading
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
from .colors import Colors

C = Colors()


class SpinnerStyle(Enum):
    DOTS = "dots"
    DOTS_SNAKE = "dots_snake"
    LINE = "line"
    PIPE = "pipe"
    ARROW = "arrow"
    BOUNCE = "bounce"
    BOX = "box"
    CIRCLE = "circle"
    SQUARE = "square"
    HAMBURGER = "hamburger"
    GROW = "grow"
    BALLOON = "balloon"
    FLIP = "flip"
    PULSE = "pulse"
    POINTS = "points"
    BRAILLE = "braille"
    NVIDIA = "nvidia"
    MATRIX = "matrix"
    BINARY = "binary"
    HEXADECIMAL = "hex"
    ASCII_DOTS = "ascii_dots"
    ASCII_LINE = "ascii_line"
    ASCII_BOX = "ascii_box"
    WAVE = "wave"
    MOON = "moon"
    EARTH = "earth"
    CLOCK = "clock"


@dataclass
class SpinnerConfig:
    frames: List[str]
    interval: float = 0.1
    color: str = ""
    success_symbol: str = "✓"
    error_symbol: str = "✗"
    warning_symbol: str = "⚠"
    info_symbol: str = "ℹ"
    silent: bool = False
    log_callback: Optional[Callable[[str], None]] = None


class SpinnerFrames:
    DOTS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    DOTS_SNAKE = ["⢀⠀", "⡀⠀", "⠄⠀", "⢂⠀", "⡂⠀", "⠅⠀", "⢃⠀", "⡃⠀", "⠍⠀", "⢋⠀", "⡋⠀", "⠍⠁", "⢋⠁", "⡋⠁", "⠍⠉", "⠋⠉"]
    LINE = ["-", "\\", "|", "/"]
    PIPE = ["┤", "┘", "┴", "└", "├", "┌", "┬", "┐"]
    ARROW = ["←", "↖", "↑", "↗", "→", "↘", "↓", "↙"]
    BOUNCE = ["⠁", "⠂", "⠄", "⡀", "⢀", "⠠", "⠐", "⠈"]
    BOX = ["◰", "◳", "◲", "◱"]
    CIRCLE = ["◐", "◓", "◑", "◒"]
    SQUARE = ["◘", "◙", "◚", "◛"]
    HAMBURGER = ["☱", "☲", "☴"]
    GROW = ["▁", "▃", "▄", "▅", "▆", "▇", "█", "▇", "▆", "▅", "▄", "▃"]
    BALLOON = [".", "o", "O", "°", "O", "o", "."]
    FLIP = ["_", "_", "_", "‾", "‾", "‾"]
    PULSE = ["◾", "◽", "▪", "▫", "▪", "◽"]
    POINTS = ["∙∙∙", "●∙∙", "∙●∙", "∙∙●", "∙∙∙"]
    BRAILLE = ["⡿", "⣟", "⣯", "⣷", "⣾", "⣽", "⣻", "⢿"]
    BRAILLE_DOTS = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]
    
    ASCII_DOTS = [".", "..", "...", "....", "...", ".."]
    ASCII_LINE = ["|", "/", "-", "\\"]
    ASCII_ARROW = ["<", "^", ">", "v"]
    ASCII_BOX = ["[=  ]", "[ = ]", "[  =]", "[ = ]"]
    ASCII_PROGRESS = ["[    ]", "[■   ]", "[■■  ]", "[■■■ ]", "[■■■■]", "[ ■■■]", "[  ■■]", "[   ■]"]
    
    NVIDIA = ["N", "NV", "NVI", "NVID", "NVIDI", "NVIDIA", "VIDIA", "IDIA", "DIA", "IA", "A", ""]
    MATRIX = ["ﾊ", "ﾐ", "ﾋ", "ｰ", "ｳ", "ｼ", "ﾅ", "ﾓ", "ﾆ", "ｻ", "ﾜ", "ﾂ", "ｵ", "ﾘ", "ｱ", "ﾎ", "ﾃ", "ﾏ", "ｹ", "ﾒ"]
    BINARY = ["0000", "0001", "0010", "0011", "0100", "0101", "0110", "0111", "1000", "1001", "1010", "1011", "1100", "1101", "1110", "1111"]
    HEXADECIMAL = ["0x0", "0x1", "0x2", "0x3", "0x4", "0x5", "0x6", "0x7", "0x8", "0x9", "0xA", "0xB", "0xC", "0xD", "0xE", "0xF"]
    WAVE = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█", "▇", "▆", "▅", "▄", "▃", "▂"]
    
    MOON = ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"]
    EARTH = ["🌍", "🌎", "🌏"]
    CLOCK = ["🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚", "🕛"]
    HEARTS = ["💛", "💙", "💜", "💚", "❤️"]
    
    @classmethod
    def get_frames(cls, style: SpinnerStyle) -> List[str]:
        mapping = {
            SpinnerStyle.DOTS: cls.DOTS,
            SpinnerStyle.DOTS_SNAKE: cls.DOTS_SNAKE,
            SpinnerStyle.LINE: cls.LINE,
            SpinnerStyle.PIPE: cls.PIPE,
            SpinnerStyle.ARROW: cls.ARROW,
            SpinnerStyle.BOUNCE: cls.BOUNCE,
            SpinnerStyle.BOX: cls.BOX,
            SpinnerStyle.CIRCLE: cls.CIRCLE,
            SpinnerStyle.SQUARE: cls.SQUARE,
            SpinnerStyle.HAMBURGER: cls.HAMBURGER,
            SpinnerStyle.GROW: cls.GROW,
            SpinnerStyle.BALLOON: cls.BALLOON,
            SpinnerStyle.FLIP: cls.FLIP,
            SpinnerStyle.PULSE: cls.PULSE,
            SpinnerStyle.POINTS: cls.POINTS,
            SpinnerStyle.BRAILLE: cls.BRAILLE,
            SpinnerStyle.NVIDIA: cls.NVIDIA,
            SpinnerStyle.MATRIX: cls.MATRIX,
            SpinnerStyle.BINARY: cls.BINARY,
            SpinnerStyle.HEXADECIMAL: cls.HEXADECIMAL,
            SpinnerStyle.ASCII_DOTS: cls.ASCII_DOTS,
            SpinnerStyle.ASCII_LINE: cls.ASCII_LINE,
            SpinnerStyle.ASCII_BOX: cls.ASCII_BOX,
            SpinnerStyle.WAVE: cls.WAVE,
            SpinnerStyle.MOON: cls.MOON,
            SpinnerStyle.EARTH: cls.EARTH,
            SpinnerStyle.CLOCK: cls.CLOCK,
        }
        return mapping.get(style, cls.DOTS)


class Spinner:
    _default_intervals = {
        SpinnerStyle.DOTS: 0.08,
        SpinnerStyle.DOTS_SNAKE: 0.06,
        SpinnerStyle.LINE: 0.13,
        SpinnerStyle.PIPE: 0.1,
        SpinnerStyle.ARROW: 0.12,
        SpinnerStyle.BOUNCE: 0.14,
        SpinnerStyle.BOX: 0.12,
        SpinnerStyle.CIRCLE: 0.13,
        SpinnerStyle.SQUARE: 0.15,
        SpinnerStyle.HAMBURGER: 0.2,
        SpinnerStyle.GROW: 0.12,
        SpinnerStyle.BALLOON: 0.14,
        SpinnerStyle.FLIP: 0.5,
        SpinnerStyle.PULSE: 0.2,
        SpinnerStyle.POINTS: 0.15,
        SpinnerStyle.BRAILLE: 0.08,
        SpinnerStyle.NVIDIA: 0.15,
        SpinnerStyle.MATRIX: 0.05,
        SpinnerStyle.BINARY: 0.1,
        SpinnerStyle.HEXADECIMAL: 0.1,
        SpinnerStyle.ASCII_DOTS: 0.2,
        SpinnerStyle.ASCII_LINE: 0.15,
        SpinnerStyle.ASCII_BOX: 0.15,
        SpinnerStyle.WAVE: 0.1,
        SpinnerStyle.MOON: 0.15,
        SpinnerStyle.EARTH: 0.3,
        SpinnerStyle.CLOCK: 0.2,
    }
    
    def __init__(
        self,
        message: str = "Loading",
        style: SpinnerStyle = SpinnerStyle.DOTS,
        color: str = None,
        show_elapsed: bool = True,
        custom_frames: List[str] = None,
        interval: float = None,
        silent: bool = False,
        on_tick: Optional[Callable[[int], None]] = None
    ):
        self.message = message
        self.style = style
        self.color = color or C.BRIGHT_CYAN
        self.show_elapsed = show_elapsed
        self.frames = custom_frames or SpinnerFrames.get_frames(style)
        self.interval = interval or self._default_intervals.get(style, 0.1)
        self.silent = silent
        self.on_tick = on_tick
        
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.frame_idx = 0
        self.tick_count = 0
        self.start_time = 0.0
        self.max_output_length = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._final_message = ""
        self._final_color = ""
    
    def _animate(self):
        while not self._stop_event.is_set():
            with self._lock:
                if not self.running:
                    break
                
                frame = self.frames[self.frame_idx % len(self.frames)]
                elapsed = ""
                
                if self.show_elapsed and self.start_time:
                    elapsed_time = time.time() - self.start_time
                    if elapsed_time >= 1:
                        elapsed = f" {C.DIM}({elapsed_time:.1f}s){C.RESET}"
                
                output = f"\r{self.color}{frame}{C.RESET} {self.message}{elapsed}  "
                self.max_output_length = max(self.max_output_length, len(output) + 10)
                
                if not self.silent:
                    try:
                        sys.stdout.write(output)
                        sys.stdout.flush()
                    except:
                        pass
                
                self.frame_idx += 1
                self.tick_count += 1
                
                if self.on_tick and self.tick_count % 10 == 0:
                    try:
                        self.on_tick(self.tick_count)
                    except:
                        pass
            
            time.sleep(self.interval)
        
        if not self.silent:
            if self._final_message:
                try:
                    sys.stdout.write(f"\r{' ' * self.max_output_length}\r")
                    sys.stdout.write(f"{self._final_color}{self._final_message}{C.RESET}\n")
                    sys.stdout.flush()
                except:
                    pass
            else:
                try:
                    sys.stdout.write(f"\r{' ' * self.max_output_length}\r")
                    sys.stdout.flush()
                except:
                    pass
    
    def start(self):
        with self._lock:
            if self.running:
                return self
            self.running = True
            self.start_time = time.time()
            self.frame_idx = 0
            self.tick_count = 0
            self.max_output_length = 0
            self._stop_event.clear()
            self._final_message = ""
            self._final_color = ""
            self.thread = threading.Thread(target=self._animate, daemon=True)
            self.thread.start()
        return self
    
    def stop(self, message: str = None, status: str = "success"):
        final_msg = ""
        final_color = ""
        
        if message:
            symbols = {
                "success": (f"{C.BRIGHT_GREEN}✓{C.RESET}", C.GREEN),
                "error": (f"{C.BRIGHT_RED}✗{C.RESET}", C.RED),
                "warning": (f"{C.BRIGHT_YELLOW}⚠{C.RESET}", C.YELLOW),
                "info": (f"{C.BRIGHT_BLUE}ℹ{C.RESET}", C.BLUE),
                "none": ("", ""),
            }
            symbol, color = symbols.get(status, ("", ""))
            final_msg = f"{symbol} {message}" if symbol else message
            final_color = color
        
        with self._lock:
            self.running = False
            self._final_message = final_msg
            self._final_color = final_color
        
        self._stop_event.set()
        
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)
    
    def update(self, message: str):
        with self._lock:
            self.message = message
    
    def get_elapsed(self) -> float:
        if self.start_time:
            return time.time() - self.start_time
        return 0.0
    
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.stop(f"Error: {exc_type.__name__}", "error")
        else:
            self.stop()
        return False


class SmartSpinner(Spinner):
    def __init__(self, message: str = "Loading", **kwargs):
        supports_unicode = self._check_unicode_support()
        supports_colors = self._check_color_support()
        
        if not supports_unicode and 'custom_frames' not in kwargs and 'style' not in kwargs:
            kwargs['custom_frames'] = SpinnerFrames.ASCII_LINE
        
        if not supports_colors:
            kwargs['color'] = ""
        
        super().__init__(message, **kwargs)
    
    def _check_unicode_support(self) -> bool:
        try:
            if sys.platform == 'win32':
                import locale
                encoding = locale.getpreferredencoding().lower()
                return 'utf' in encoding or 'cp65001' in encoding
            return True
        except:
            return False
    
    def _check_color_support(self) -> bool:
        if os.getenv('NO_COLOR'):
            return False
        
        if os.getenv('FORCE_COLOR'):
            return True
        
        if sys.platform == 'win32':
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
                return True
            except:
                return False
        
        return hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()


class ThinkingSpinner(Spinner):
    def __init__(
        self, 
        message: str = None,
        style: SpinnerStyle = None,
        color: str = None,
        show_elapsed: bool = True,
        **kwargs
    ):
        super().__init__(
            message=message or "Pensando",
            style=style or SpinnerStyle.DOTS,
            color=color or C.BRIGHT_CYAN,
            show_elapsed=show_elapsed,
            **kwargs
        )


class ToolSpinner(Spinner):
    def __init__(
        self, 
        tool_name: str,
        style: SpinnerStyle = None,
        color: str = None,
        **kwargs
    ):
        self.tool_name = tool_name
        super().__init__(
            message=f"Ejecutando {tool_name}",
            style=style or SpinnerStyle.CIRCLE,
            color=color or C.BRIGHT_YELLOW,
            show_elapsed=True,
            **kwargs
        )
    
    def complete(self, success: bool = True, message: str = None):
        if success:
            final_message = message or f"Completado {self.tool_name}"
            self.stop(final_message, "success")
        else:
            final_message = message or f"Error en {self.tool_name}"
            self.stop(final_message, "error")


class StreamingIndicator:
    def __init__(self, style: str = "wave", color: str = None, message: str = "Streaming"):
        self.style = style
        self.color = color or C.BRIGHT_CYAN
        self.message = message
        self.active = False
        self.thread = None
        self.frame_idx = 0
        self.max_length = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        
        self.animations = {
            "wave": ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█", "▇", "▆", "▅", "▄", "▃", "▂"],
            "dots": ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"],
            "pulse": ["░", "▒", "▓", "█", "▓", "▒"],
            "flow": ["◉", "○", "◎", "○"],
            "typing": ["▏", "▎", "▍", "▌", "▋", "▊", "▉", "█", "▉", "▊", "▋", "▌", "▍", "▎"],
            "bounce": ["⠁", "⠂", "⠄", "⠂"],
            "snake": ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"],
        }
    
    def _animate(self):
        frames = self.animations.get(self.style, self.animations["wave"])
        
        while not self._stop_event.is_set():
            with self._lock:
                if not self.active:
                    break
                
                frame = frames[self.frame_idx % len(frames)]
                output = f"\r{self.color}{self.message} {frame}{C.RESET} "
                self.max_length = max(self.max_length, len(output) + 5)
                
                try:
                    sys.stdout.write(output)
                    sys.stdout.flush()
                except:
                    pass
                
                self.frame_idx += 1
            
            time.sleep(0.1)
        
        try:
            sys.stdout.write(f"\r{' ' * self.max_length}\r")
            sys.stdout.flush()
        except:
            pass
    
    def start(self):
        with self._lock:
            if self.active:
                return self
            self.active = True
            self.frame_idx = 0
            self.max_length = 0
            self._stop_event.clear()
            self.thread = threading.Thread(target=self._animate, daemon=True)
            self.thread.start()
        return self
    
    def stop(self):
        with self._lock:
            self.active = False
        
        self._stop_event.set()
        
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=0.5)
    
    def pulse(self):
        if not self.active:
            self.start()
    
    def update(self, message: str):
        with self._lock:
            self.message = message
    
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False


class ProgressBar:
    def __init__(
        self,
        total: int,
        message: str = "Progress",
        width: int = 40,
        style: str = "blocks",
        show_percentage: bool = True,
        show_time: bool = True,
        show_count: bool = False,
        color: str = None
    ):
        if total <= 0:
            total = 1
        
        self.total = total
        self.current = 0
        self.message = message
        self.width = width
        self.style = style
        self.show_percentage = show_percentage
        self.show_time = show_time
        self.show_count = show_count
        self.color = color or C.BRIGHT_CYAN
        self.start_time = time.time()
        self.completed = False
        
        self.styles = {
            "blocks": ("█", "░"),
            "lines": ("━", "─"),
            "dots": ("●", "○"),
            "arrows": ("▶", "▷"),
            "squares": ("■", "□"),
            "shades": ("█", "▓", "▒", "░"),
            "ascii": ("#", "-"),
            "equals": ("=", " "),
        }
    
    def update(self, current: int = None, increment: int = 1, message: str = None):
        if self.completed:
            return
        
        if current is not None:
            self.current = min(current, self.total)
        else:
            self.current = min(self.current + increment, self.total)
        
        if message:
            self.message = message
        
        self._render()
    
    def _render(self):
        progress = min(1.0, self.current / self.total)
        filled_width = int(progress * self.width)
        
        if self.style == "shades":
            chars = self.styles["shades"]
            partial = (progress * self.width) % 1
            
            bar = chars[0] * filled_width
            
            if filled_width < self.width:
                if partial >= 0.75:
                    bar += chars[1]
                elif partial >= 0.5:
                    bar += chars[2]
                elif partial >= 0.25:
                    bar += chars[3]
                else:
                    bar += chars[3]
                bar += chars[3] * (self.width - filled_width - 1)
            
        else:
            filled_char, empty_char = self.styles.get(self.style, self.styles["blocks"])
            bar = filled_char * filled_width + empty_char * (self.width - filled_width)
        
        output = f"\r{C.DIM}{self.message}:{C.RESET} "
        output += f"{self.color}[{bar}]{C.RESET}"
        
        if self.show_percentage:
            output += f" {C.BRIGHT_WHITE}{progress*100:5.1f}%{C.RESET}"
        
        if self.show_count:
            output += f" {C.DIM}({self.current}/{self.total}){C.RESET}"
        
        if self.show_time:
            elapsed = time.time() - self.start_time
            
            if progress > 0 and progress < 1:
                eta = (elapsed / progress) * (1 - progress)
                output += f" {C.DIM}ETA: {self._format_time(eta)}{C.RESET}"
            elif progress >= 1:
                output += f" {C.GREEN}✓ {self._format_time(elapsed)}{C.RESET}"
        
        output += "  "
        
        try:
            sys.stdout.write(output)
            sys.stdout.flush()
        except:
            pass
        
        if progress >= 1:
            self.completed = True
            print()
    
    def _format_time(self, seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.1f}s"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            secs = int(seconds % 60)
            return f"{minutes}m {secs}s"
        else:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours}h {minutes}m"
    
    def complete(self, message: str = None):
        self.current = self.total
        if message:
            self.message = message
        self._render()
    
    def reset(self, total: int = None, message: str = None):
        if total:
            self.total = total
        if message:
            self.message = message
        self.current = 0
        self.start_time = time.time()
        self.completed = False


class MultiStepProgressBar:
    def __init__(self, steps: List[str], width: int = 30):
        self.steps = steps
        self.current_step = 0
        self.step_progress: Dict[str, float] = {}
        self.width = width
        self.start_time = time.time()
        self._lines_rendered = 0
        
        for step in steps:
            self.step_progress[step] = 0.0
    
    def start_step(self, step_name: str):
        if step_name in self.steps:
            self.current_step = self.steps.index(step_name)
            self._render()
    
    def update_step(self, step_name: str, progress: float):
        if step_name in self.step_progress:
            self.step_progress[step_name] = min(1.0, max(0.0, progress))
            self._render()
    
    def complete_step(self, step_name: str):
        self.update_step(step_name, 1.0)
        if step_name in self.steps:
            idx = self.steps.index(step_name)
            if idx < len(self.steps) - 1:
                self.current_step = idx + 1
    
    def _render(self):
        if self._lines_rendered > 0:
            for _ in range(self._lines_rendered):
                sys.stdout.write("\033[F\033[K")
        
        output_lines = []
        
        for i, step in enumerate(self.steps):
            progress = self.step_progress[step]
            
            if progress >= 1.0:
                symbol = f"{C.BRIGHT_GREEN}✓{C.RESET}"
                bar_color = C.GREEN
            elif i == self.current_step:
                symbol = f"{C.BRIGHT_CYAN}►{C.RESET}"
                bar_color = C.BRIGHT_CYAN
            elif progress > 0:
                symbol = f"{C.BRIGHT_YELLOW}○{C.RESET}"
                bar_color = C.YELLOW
            else:
                symbol = f"{C.DIM}○{C.RESET}"
                bar_color = C.DIM
            
            filled = int(progress * self.width)
            bar = "█" * filled + "░" * (self.width - filled)
            
            percent = f"{progress*100:5.1f}%"
            line = f"{symbol} {step:20} [{bar_color}{bar}{C.RESET}] {percent}"
            output_lines.append(line)
        
        overall = sum(self.step_progress.values()) / len(self.steps)
        elapsed = time.time() - self.start_time
        output_lines.append(f"{C.DIM}Overall: {overall*100:.1f}% | Elapsed: {elapsed:.1f}s{C.RESET}")
        
        self._lines_rendered = len(output_lines)
        
        try:
            print("\n".join(output_lines))
            sys.stdout.flush()
        except:
            pass
    
    def complete_all(self):
        for step in self.steps:
            self.step_progress[step] = 1.0
        self.current_step = len(self.steps) - 1
        self._render()


class MultiSpinner:
    def __init__(self):
        self.spinners: Dict[str, Spinner] = {}
        self.active_count = 0
        self._lock = threading.Lock()
    
    def add(
        self, 
        key: str, 
        message: str, 
        style: SpinnerStyle = SpinnerStyle.DOTS,
        color: str = None
    ) -> Spinner:
        with self._lock:
            if key in self.spinners:
                self.spinners[key].stop()
            
            spinner = Spinner(message, style, color=color)
            self.spinners[key] = spinner
            return spinner
    
    def start(self, key: str):
        with self._lock:
            if key in self.spinners and not self.spinners[key].running:
                self.spinners[key].start()
                self.active_count += 1
    
    def stop(self, key: str, message: str = None, status: str = "success"):
        with self._lock:
            if key in self.spinners:
                if self.spinners[key].running:
                    self.active_count -= 1
                spinner = self.spinners[key]
        
        if spinner:
            spinner.stop(message, status)
    
    def update(self, key: str, message: str):
        with self._lock:
            if key in self.spinners:
                self.spinners[key].update(message)
    
    def stop_all(self, wait: bool = True):
        spinners_to_stop = []
        
        with self._lock:
            spinners_to_stop = list(self.spinners.values())
            self.active_count = 0
        
        for spinner in spinners_to_stop:
            spinner.stop()
        
        if wait:
            for spinner in spinners_to_stop:
                if spinner.thread and spinner.thread.is_alive():
                    spinner.thread.join(timeout=0.5)
        
        with self._lock:
            self.spinners.clear()
    
    def get(self, key: str) -> Optional[Spinner]:
        with self._lock:
            return self.spinners.get(key)
    
    def __len__(self) -> int:
        return len(self.spinners)


class CountdownTimer:
    def __init__(
        self, 
        seconds: int, 
        message: str = "Time remaining",
        on_complete: Optional[Callable] = None,
        color: str = None
    ):
        self.total_seconds = seconds
        self.remaining = seconds
        self.message = message
        self.on_complete = on_complete
        self.color = color or C.BRIGHT_YELLOW
        self.running = False
        self.thread = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
    
    def _countdown(self):
        while not self._stop_event.is_set() and self.remaining > 0:
            with self._lock:
                if not self.running:
                    break
                
                minutes, seconds = divmod(self.remaining, 60)
                hours, minutes = divmod(minutes, 60)
                
                if hours > 0:
                    time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                else:
                    time_str = f"{minutes:02d}:{seconds:02d}"
                
                output = f"\r{self.color}⏱ {self.message}: {time_str}{C.RESET}  "
                
                try:
                    sys.stdout.write(output)
                    sys.stdout.flush()
                except:
                    pass
                
                self.remaining -= 1
            
            time.sleep(1)
        
        if self.remaining <= 0 and self.running:
            try:
                sys.stdout.write(f"\r{C.BRIGHT_GREEN}✓ {self.message}: Complete!{C.RESET}   \n")
                sys.stdout.flush()
            except:
                pass
            
            if self.on_complete:
                try:
                    self.on_complete()
                except:
                    pass
    
    def start(self):
        with self._lock:
            if self.running:
                return self
            self.running = True
            self._stop_event.clear()
            self.thread = threading.Thread(target=self._countdown, daemon=True)
            self.thread.start()
        return self
    
    def stop(self):
        with self._lock:
            self.running = False
        
        self._stop_event.set()
        
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)
    
    def reset(self, seconds: int = None):
        with self._lock:
            self.remaining = seconds or self.total_seconds
    
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False


class StatusIndicator:
    def __init__(self):
        self._current_line_length = 0
    
    def show(self, message: str, status: str = "info"):
        symbols = {
            "success": (f"{C.BRIGHT_GREEN}✓{C.RESET}", C.GREEN),
            "error": (f"{C.BRIGHT_RED}✗{C.RESET}", C.RED),
            "warning": (f"{C.BRIGHT_YELLOW}⚠{C.RESET}", C.YELLOW),
            "info": (f"{C.BRIGHT_BLUE}ℹ{C.RESET}", C.BLUE),
            "working": (f"{C.BRIGHT_CYAN}⋯{C.RESET}", C.CYAN),
            "pending": (f"{C.DIM}○{C.RESET}", C.DIM),
            "done": (f"{C.BRIGHT_GREEN}●{C.RESET}", C.GREEN),
        }
        
        symbol, color = symbols.get(status, (f"{C.DIM}•{C.RESET}", ""))
        output = f"{symbol} {message}"
        
        print(output)
    
    def update_line(self, message: str, status: str = "info"):
        symbols = {
            "success": (f"{C.BRIGHT_GREEN}✓{C.RESET}", C.GREEN),
            "error": (f"{C.BRIGHT_RED}✗{C.RESET}", C.RED),
            "warning": (f"{C.BRIGHT_YELLOW}⚠{C.RESET}", C.YELLOW),
            "info": (f"{C.BRIGHT_BLUE}ℹ{C.RESET}", C.BLUE),
            "working": (f"{C.BRIGHT_CYAN}⋯{C.RESET}", C.CYAN),
        }
        
        symbol, _ = symbols.get(status, (f"{C.DIM}•{C.RESET}", ""))
        output = f"\r{symbol} {message}"
        
        if len(output) < self._current_line_length:
            output += " " * (self._current_line_length - len(output))
        
        self._current_line_length = len(output)
        
        try:
            sys.stdout.write(output)
            sys.stdout.flush()
        except:
            pass
    
    def finish_line(self, message: str = None, status: str = "success"):
        if message:
            self.update_line(message, status)
        print()
        self._current_line_length = 0


def with_spinner(
    message: str = "Processing", 
    style: SpinnerStyle = SpinnerStyle.DOTS,
    color: str = None
):
    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            with Spinner(message, style, color=color):
                return func(*args, **kwargs)
        return wrapper
    return decorator


def thinking(message: str = "Thinking", color: str = None):
    return ThinkingSpinner(message, color=color)


def loading_tool(tool_name: str, color: str = None):
    return ToolSpinner(tool_name, color=color)


def timed_operation(message: str = "Processing", show_elapsed: bool = True):
    return Spinner(message, show_elapsed=show_elapsed)


_global_indicator = StatusIndicator()


def status(message: str, status_type: str = "info"):
    _global_indicator.show(message, status_type)


def success(message: str):
    _global_indicator.show(message, "success")


def error(message: str):
    _global_indicator.show(message, "error")


def warning(message: str):
    _global_indicator.show(message, "warning")


def info(message: str):
    _global_indicator.show(message, "info")


if __name__ == "__main__":
    import random
    
    print("=== SPINNER DEMO ===\n")
    
    styles = [
        SpinnerStyle.DOTS,
        SpinnerStyle.CIRCLE,
        SpinnerStyle.BRAILLE,
        SpinnerStyle.NVIDIA,
    ]
    
    for style in styles:
        spinner = Spinner(f"Testing {style.value}", style)
        spinner.start()
        time.sleep(1.5)
        spinner.stop(f"Done with {style.value}", "success")
        time.sleep(0.3)
    
    print("\n=== PROGRESS BAR DEMO ===\n")
    
    progress = ProgressBar(100, "Downloading", show_count=True)
    for i in range(101):
        progress.update(i)
        time.sleep(0.02)
    
    print("\n=== MULTI-STEP PROGRESS DEMO ===\n")
    
    steps = MultiStepProgressBar(["Download", "Extract", "Install", "Configure"])
    
    for i, step in enumerate(["Download", "Extract", "Install", "Configure"]):
        steps.start_step(step)
        for p in range(0, 101, 10):
            steps.update_step(step, p / 100)
            time.sleep(0.05)
        steps.complete_step(step)
    
    print("\n=== STATUS INDICATOR DEMO ===\n")
    
    success("Operation completed successfully")
    error("Failed to connect")
    warning("Low disk space")
    info("Starting process...")
    
    print("\n=== COUNTDOWN DEMO ===\n")
    
    countdown = CountdownTimer(5, "Starting in")
    countdown.start()
    time.sleep(6)
    
    print("\nDemo completed!")