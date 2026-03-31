import os
import sys
import shutil
from typing import List, Optional, Tuple
from datetime import datetime
from .colors import Colors

C = Colors()

HAS_THEMES = False

try:
    from .themes import get_current_theme, get_theme_manager
    HAS_THEMES = True
except ImportError:
    pass


def _rgb(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"


def _bg_rgb(r: int, g: int, b: int) -> str:
    return f"\033[48;2;{r};{g};{b}m"


def _get_default_gradient() -> Tuple[Tuple[int,int,int], Tuple[int,int,int]]:
    return (118, 185, 0), (0, 200, 150)


def _get_theme_gradient() -> Tuple[Tuple[int,int,int], Tuple[int,int,int]]:
    if not HAS_THEMES:
        return _get_default_gradient()
    try:
        theme = get_current_theme()
        return theme.gradient_start, theme.gradient_end
    except:
        return _get_default_gradient()


def _get_theme_colors() -> dict:
    defaults = {
        'primary': (118, 185, 0),
        'dim': (128, 128, 128),
        'separator_char': '═',
        'gradient_start': (118, 185, 0),
        'gradient_end': (0, 200, 150),
    }
    if not HAS_THEMES:
        return defaults
    try:
        theme = get_current_theme()
        return {
            'primary': theme.primary,
            'dim': getattr(theme, 'dim', (128, 128, 128)),
            'separator_char': getattr(theme, 'separator_char', '═'),
            'gradient_start': theme.gradient_start,
            'gradient_end': theme.gradient_end,
        }
    except:
        return defaults


def _get_terminal_width() -> int:
    try:
        return shutil.get_terminal_size().columns
    except:
        return 100


def _gradient_line(text: str, start_rgb: tuple, end_rgb: tuple) -> str:
    if not text:
        return text
    result = []
    visible_chars = sum(1 for c in text if c != ' ')
    if visible_chars <= 1:
        r, g, b = start_rgb
        return f"{_rgb(r, g, b)}{text}{C.RESET}"
    char_idx = 0
    for char in text:
        if char == ' ':
            result.append(char)
        else:
            ratio = char_idx / (visible_chars - 1)
            r = int(start_rgb[0] + (end_rgb[0] - start_rgb[0]) * ratio)
            g = int(start_rgb[1] + (end_rgb[1] - start_rgb[1]) * ratio)
            b = int(start_rgb[2] + (end_rgb[2] - start_rgb[2]) * ratio)
            result.append(f"{_rgb(r, g, b)}{char}")
            char_idx += 1
    result.append(C.RESET)
    return ''.join(result)


def _diagonal_gradient(lines: list, start_rgb: tuple, end_rgb: tuple) -> str:
    if not lines:
        return ""
    result = []
    total_lines = len(lines)
    max_width = max((len(line) for line in lines), default=0)
    if total_lines <= 1 or max_width <= 1:
        for line in lines:
            result.append(_gradient_line(line, start_rgb, end_rgb))
        return "\n".join(result)
    for i, line in enumerate(lines):
        line_chars = []
        for j, char in enumerate(line):
            if char == ' ':
                line_chars.append(char)
            else:
                ratio = ((i / (total_lines - 1)) + (j / (max_width - 1))) / 2
                ratio = min(1.0, max(0.0, ratio))
                r = int(start_rgb[0] + (end_rgb[0] - start_rgb[0]) * ratio)
                g = int(start_rgb[1] + (end_rgb[1] - start_rgb[1]) * ratio)
                b = int(start_rgb[2] + (end_rgb[2] - start_rgb[2]) * ratio)
                line_chars.append(f"{_rgb(r, g, b)}{char}")
        line_chars.append(C.RESET)
        result.append(''.join(line_chars))
    return "\n".join(result)


def _radial_gradient(lines: list, center_rgb: tuple, edge_rgb: tuple) -> str:
    if not lines:
        return ""
    total_lines = len(lines)
    max_width = max((len(line) for line in lines), default=0)
    if total_lines <= 1 or max_width <= 1:
        return _diagonal_gradient(lines, center_rgb, edge_rgb)
    center_y = total_lines / 2
    center_x = max_width / 2
    max_dist = ((center_y ** 2) + (center_x ** 2)) ** 0.5
    result = []
    for i, line in enumerate(lines):
        line_chars = []
        for j, char in enumerate(line):
            if char == ' ':
                line_chars.append(char)
            else:
                dist = (((i - center_y) ** 2) + ((j - center_x) ** 2)) ** 0.5
                ratio = min(1.0, dist / max_dist) if max_dist > 0 else 0
                r = int(center_rgb[0] + (edge_rgb[0] - center_rgb[0]) * ratio)
                g = int(center_rgb[1] + (edge_rgb[1] - center_rgb[1]) * ratio)
                b = int(center_rgb[2] + (edge_rgb[2] - center_rgb[2]) * ratio)
                line_chars.append(f"{_rgb(r, g, b)}{char}")
        line_chars.append(C.RESET)
        result.append(''.join(line_chars))
    return "\n".join(result)


def _visible_len(text: str) -> int:
    import re
    clean = re.sub(r'\x1b\[[0-9;]*m', '', text)
    length = 0
    for char in clean:
        if ord(char) > 0xFFFF:
            length += 2
        else:
            length += 1
    return length


def _pad_right(text: str, width: int) -> str:
    current = _visible_len(text)
    if current >= width:
        return text
    return text + ' ' * (width - current)


def _center_text(text: str, width: int) -> str:
    current = _visible_len(text)
    if current >= width:
        return text
    left = (width - current) // 2
    right = width - current - left
    return ' ' * left + text + ' ' * right


NVIDIA_EYE_SMALL = [
    "    ▄█████████▄    ",
    "  ███████████████  ",
    " ████▀▀   ████████",
    "████ ████▄  ███████",
    "███ ████████ ██████",
    "███ ████████ ██████",
    "████ ████▀  ███████",
    " ████▄▄  █████████",
    "  ███████████████  ",
    "    ▀█████████▀    ",
]

NVIDIA_EYE_TINY = [
    "   ▄████▄   ",
    " ████▀█████ ",
    "████ ███████",
    " ████▄█████ ",
    "   ▀████▀   ",
]

LOGO_FULL = [
    "                                                                                              ",
    "            ██████████████████████               ███╗   ██╗██╗   ██╗██╗██████╗ ██╗ █████╗     ",
    "        █████████████████████████████████        ████╗  ██║██║   ██║██║██╔══██╗██║██╔══██╗    ",
    "      ██████████          ███████████████████    ██╔██╗ ██║██║   ██║██║██║  ██║██║███████║    ",
    "    ████████  ████████████     ███████████████   ██║╚██╗██║╚██╗ ██╔╝██║██║  ██║██║██╔══██║    ",
    "   ██████   █████████████████     ████████████   ██║ ╚████║ ╚████╔╝ ██║██████╔╝██║██║  ██║    ",
    "  ██████   ████████  ███████████    ██████████   ╚═╝  ╚═══╝  ╚═══╝  ╚═╝╚═════╝ ╚═╝╚═╝  ╚═╝    ",
    "  █████   ████████████████████████████  ██████                                                ",
    " ██████   █████████████████████████    ███████    ██████╗ ██████╗ ██████╗ ███████╗            ",
    "  █████    ██████████████████████   ██████████   ██╔════╝██╔═══██╗██╔══██╗██╔════╝            ",
    "  ██████    ████████████████████  ████████████   ██║     ██║   ██║██║  ██║█████╗              ",
    "   ███████     ██████████████   ██████████████   ██║     ██║   ██║██║  ██║██╔══╝              ",
    "    █████████       █████    █████████████████   ╚██████╗╚██████╔╝██████╔╝███████╗            ",
    "      ██████████████     █████████████████████    ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝            ",
    "        ██████████████████████████████████                                                    ",
    "            ██████████████████████████            ⚡ AI-Powered Development Assistant ⚡       ",
    "                                                                                              ",
]

LOGO_HEAVY = [
    "                                                                                              ",
    "            ██████████████████████               ██╗  ██╗███████╗ █████╗ ██╗   ██╗██╗   ██╗   ",
    "        █████████████████████████████████        ██║  ██║██╔════╝██╔══██╗██║   ██║╚██╗ ██╔╝   ",
    "      ██████████          ███████████████████    ███████║█████╗  ███████║██║   ██║ ╚████╔╝    ",
    "    ████████  ████████████     ███████████████   ██╔══██║██╔══╝  ██╔══██║╚██╗ ██╔╝  ╚██╔╝     ",
    "   ██████   █████████████████     ████████████   ██║  ██║███████╗██║  ██║ ╚████╔╝    ██║      ",
    "  ██████   ████████  ███████████    ██████████   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝  ╚═══╝     ╚═╝      ",
    "  █████   ████████████████████████████  ██████                                                ",
    " ██████   █████████████████████████    ███████    █████╗  ██████╗ ███████╗███╗   ██╗████████╗ ",
    "  █████    ██████████████████████   ██████████   ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝ ",
    "  ██████    ████████████████████  ████████████   ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║    ",
    "   ███████     ██████████████   ██████████████   ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║    ",
    "    █████████       █████    █████████████████   ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║    ",
    "      ██████████████     █████████████████████   ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝    ",
    "        ██████████████████████████████████                                                    ",
    "            ██████████████████████████                                                        ",
    "                                                                                              ",
    "    ══════════════════════════════════════════════════════════════════════════════════════    ",
    "                            🔥 MULTI-AI COLLABORATION SYSTEM 🔥                               ",
    "                                                                                              ",
]


LOGO_MAP = {
    "default": LOGO_FULL,
    "full": LOGO_FULL,
    "heavy": LOGO_HEAVY,
}


VERSION = "2.0.0"


def render_welcome_screen(
    model_name: str = "GLM 5",
    model_specialty: str = "Razonamiento",
    directory: str = None,
    heavy_mode: bool = False,
    auto_mode: bool = False,
    plugins_count: int = 0,
    tips: List[str] = None,
    recent_chats: List[dict] = None,
    version: str = None,
) -> str:
    start_rgb, end_rgb = _get_theme_gradient()
    colors = _get_theme_colors()
    primary = _rgb(*colors['primary'])
    dim = _rgb(*colors['dim'])
    border = _rgb(*start_rgb)
    ver = version or VERSION
    directory = directory or os.getcwd()

    LW = 48
    RW = 55
    TW = LW + RW + 3

    if recent_chats is None:
        recent_chats = []
    if tips is None:
        tips = [
            "Run /help to see all commands",
            "Use /model <n> to switch models",
            "Try /themes to change look",
            "Use /heavy for multi-AI mode",
        ]

    dir_display = directory
    if len(dir_display) > LW - 6:
        dir_display = "..." + dir_display[-(LW - 9):]

    modes = []
    if heavy_mode:
        modes.append("Heavy")
    if auto_mode:
        modes.append("Auto")
    if plugins_count > 0:
        modes.append(f"{plugins_count} plugins")
    modes_text = " | ".join(modes) if modes else ""

    header = f" NVIDIA Code v{ver} "
    hfill = TW - len(header)
    hl = hfill // 2
    hr = hfill - hl

    lines_out = []
    lines_out.append(_gradient_line(f"╭{'─' * hl}{header}{'─' * hr}╮", start_rgb, end_rgb))

    def R(left_plain: str, left_fmt: str, right_plain: str, right_fmt: str):
        lpad = max(0, LW - len(left_plain))
        rpad = max(0, RW - len(right_plain))
        lines_out.append(
            f"{border}│{C.RESET} {left_fmt}{' ' * lpad}{dim}│{C.RESET} {right_fmt}{' ' * rpad}{border}│{C.RESET}"
        )

    def empty():
        R("", "", "", "")

    def cpad(text: str, width: int) -> str:
        p = max(0, (width - len(text)) // 2)
        return " " * p

    eye = [
        "    ▄█████████▄    ",
        "  ███████████████  ",
        " ████▀▀   ████████",
        "████ ████▄  ███████",
        "███ ████████ ██████",
        "███ ████████ ██████",
        "████ ████▀  ███████",
        " ████▄▄  █████████",
        "  ███████████████  ",
        "    ▀█████████▀    ",
    ]
    eye_pad = (LW - 19) // 2

    left_data = []
    right_data = []

    left_data.append(("", ""))
    right_data.append(("", ""))

    welcome = "Welcome to NVIDIA Code!"
    wp = cpad(welcome, LW)
    welcome_plain = wp + welcome
    welcome_fmt = f"{wp}{C.BOLD}{welcome}{C.RESET}"
    left_data.append((welcome_plain, welcome_fmt))
    right_data.append(("Tips for getting started", f"{C.BOLD}{primary}Tips for getting started{C.RESET}"))

    left_data.append(("", ""))
    right_data.append(("", ""))

    for i, eline in enumerate(eye):
        plain = " " * eye_pad + eline
        fmt = " " * eye_pad + _gradient_line(eline, start_rgb, end_rgb)
        left_data.append((plain, fmt))
        if i < len(tips):
            tip = tips[i]
            right_data.append((f" * {tip}", f" {dim}*{C.RESET} {tip}"))
        else:
            right_data.append(("", ""))

    left_data.append(("", ""))
    right_data.append(("", ""))

    model_plain = f"{model_name} | {model_specialty}"
    mp = cpad(model_plain, LW)
    model_plain_padded = mp + model_plain
    model_fmt = f"{mp}{primary}{C.BOLD}{model_name}{C.RESET}{dim} | {model_specialty}{C.RESET}"
    left_data.append((model_plain_padded, model_fmt))
    sep_line = "─" * (RW - 2)
    right_data.append((sep_line, f"{dim}{sep_line}{C.RESET}"))

    dp = cpad(dir_display, LW)
    dir_plain_padded = dp + dir_display
    dir_fmt = f"{dp}{dim}{dir_display}{C.RESET}"
    left_data.append((dir_plain_padded, dir_fmt))
    right_data.append(("", ""))

    left_data.append(("", ""))
    right_data.append(("Recent activity", f"{C.BOLD}{primary}Recent activity{C.RESET}"))

    if modes_text:
        mp2 = cpad(modes_text, LW)
        modes_plain_padded = mp2 + modes_text
        parts_fmt = []
        for m in modes:
            if m == "Heavy":
                parts_fmt.append(f"{C.BRIGHT_MAGENTA}{m}{C.RESET}")
            elif m == "Auto":
                parts_fmt.append(f"{C.BRIGHT_GREEN}{m}{C.RESET}")
            else:
                parts_fmt.append(f"{C.BRIGHT_CYAN}{m}{C.RESET}")
        modes_fmt = f"{mp2}" + f"{dim} | {C.RESET}".join(parts_fmt)
        left_data.append((modes_plain_padded, modes_fmt))
    else:
        left_data.append(("", ""))

    if recent_chats:
        for chat in recent_chats[:3]:
            name = chat.get('name', 'Unknown')[:25]
            msgs = chat.get('messages', 0)
            plain = f" * {name} ({msgs} msgs)"
            fmt = f" {dim}*{C.RESET} {name} {dim}({msgs} msgs){C.RESET}"
            right_data.append((plain, fmt))
    else:
        right_data.append((" No recent activity", f" {dim}No recent activity{C.RESET}"))

    left_data.append(("", ""))
    right_data.append(("", ""))

    mx = max(len(left_data), len(right_data))
    while len(left_data) < mx:
        left_data.append(("", ""))
    while len(right_data) < mx:
        right_data.append(("", ""))

    for i in range(mx):
        lp, lf = left_data[i]
        rp, rf = right_data[i]
        R(lp, lf, rp, rf)

    lines_out.append(_gradient_line(f"╰{'─' * TW}╯", start_rgb, end_rgb))

    return "\n".join(lines_out)

def render_logo(style: str = None, gradient_type: str = "diagonal") -> str:
    start_rgb, end_rgb = _get_theme_gradient()

    if style == "minimal":
        colors = _get_theme_colors()
        sep_char = colors.get('separator_char', '═')
        sep = _gradient_line(sep_char * 60, start_rgb, end_rgb)
        title = _gradient_line("                         NVIDIA CODE", start_rgb, end_rgb)
        dim_c = colors['dim']
        subtitle = f"{_rgb(*dim_c)}              AI-Powered Development Assistant{C.RESET}"
        return f"\n{sep}\n{title}\n{subtitle}\n{sep}\n"

    logo_lines = LOGO_MAP.get(style or "default", LOGO_FULL)

    if gradient_type == "radial":
        return _radial_gradient(logo_lines, start_rgb, end_rgb)
    elif gradient_type == "horizontal":
        result = []
        for line in logo_lines:
            result.append(_gradient_line(line, start_rgb, end_rgb))
        return "\n".join(result)
    else:
        return _diagonal_gradient(logo_lines, start_rgb, end_rgb)


def render_heavy_logo(gradient_type: str = "diagonal") -> str:
    start_rgb, end_rgb = _get_theme_gradient()
    lines = LOGO_HEAVY.copy()
    result = _diagonal_gradient(lines, start_rgb, end_rgb)

    if HAS_THEMES:
        try:
            theme = get_current_theme()
            agent_colors = [
                getattr(theme, 'agent_1', (255, 100, 100)),
                getattr(theme, 'agent_2', (100, 255, 100)),
                getattr(theme, 'agent_3', (100, 100, 255)),
                getattr(theme, 'synthesizer', (255, 215, 0)),
            ]
            agent_line = (
                f"           {_rgb(*agent_colors[0])}● AGENT 1{C.RESET}         "
                f"{_rgb(*agent_colors[1])}● AGENT 2{C.RESET}         "
                f"{_rgb(*agent_colors[2])}● AGENT 3{C.RESET}         "
                f"{_rgb(*agent_colors[3])}★ SYNTHESIZER{C.RESET}"
            )
            result += "\n" + agent_line
        except:
            pass

    return result + "\n"


def gradient_separator(width: int = 80) -> str:
    colors = _get_theme_colors()
    char = colors.get('separator_char', '═')
    start_rgb, end_rgb = colors['gradient_start'], colors['gradient_end']
    return _gradient_line(char * width, start_rgb, end_rgb)


def render_box(title: str, content: List[str], width: int = 60) -> str:
    start_rgb, end_rgb = _get_theme_gradient()
    top = _gradient_line(f"╔{'═' * (width - 2)}╗", start_rgb, end_rgb)
    bottom = _gradient_line(f"╚{'═' * (width - 2)}╝", start_rgb, end_rgb)
    border_color = _rgb(*start_rgb)
    lines = [top]
    if title:
        title_padded = f" {title} ".center(width - 4)
        lines.append(f"{border_color}║{C.RESET} {C.BOLD}{title_padded}{C.RESET} {border_color}║{C.RESET}")
        separator = _gradient_line(f"╟{'─' * (width - 2)}╢", start_rgb, end_rgb)
        lines.append(separator)
    for line in content:
        vis = _visible_len(line)
        padding = max(0, width - 4 - vis)
        lines.append(f"{border_color}║{C.RESET} {line}{' ' * padding} {border_color}║{C.RESET}")
    lines.append(bottom)
    return "\n".join(lines)


def render_banner(text: str, style: str = "simple") -> str:
    start_rgb, end_rgb = _get_theme_gradient()
    if style == "boxed":
        width = len(text) + 6
        top = _gradient_line(f"╔{'═' * (width - 2)}╗", start_rgb, end_rgb)
        middle = _gradient_line(f"║  {text}  ║", start_rgb, end_rgb)
        bottom = _gradient_line(f"╚{'═' * (width - 2)}╝", start_rgb, end_rgb)
        return f"{top}\n{middle}\n{bottom}"
    elif style == "underline":
        title = _gradient_line(text, start_rgb, end_rgb)
        underline = _gradient_line("═" * len(text), start_rgb, end_rgb)
        return f"{title}\n{underline}"
    else:
        return _gradient_line(text, start_rgb, end_rgb)


def print_logo(style: str = None, gradient_type: str = "diagonal"):
    if style == "welcome":
        print(render_welcome_screen())
    else:
        print(render_logo(style, gradient_type))


def print_heavy_logo(gradient_type: str = "diagonal"):
    print(render_heavy_logo(gradient_type))


def print_separator(width: int = 80):
    print(gradient_separator(width))


def print_welcome(
    model_name: str = "GLM 5",
    model_specialty: str = "🧠 Razonamiento",
    directory: str = None,
    heavy_mode: bool = False,
    auto_mode: bool = False,
    plugins_count: int = 0,
    recent_chats: List[dict] = None,
    version: str = None,
):
    print(render_welcome_screen(
        model_name=model_name,
        model_specialty=model_specialty,
        directory=directory,
        heavy_mode=heavy_mode,
        auto_mode=auto_mode,
        plugins_count=plugins_count,
        recent_chats=recent_chats,
        version=version,
    ))


def print_box(title: str, content: List[str], width: int = 60):
    print(render_box(title, content, width))


def print_banner(text: str, style: str = "simple"):
    print(render_banner(text, style))


def get_available_styles() -> List[str]:
    return list(LOGO_MAP.keys()) + ["minimal", "welcome"]


NVIDIA_LOGO = None
HEAVY_LOGO = None
MINI_LOGO = f"{C.NVIDIA_GREEN}[NVIDIA]{C.RESET} "


def _lazy_init_logos():
    global NVIDIA_LOGO, HEAVY_LOGO
    if NVIDIA_LOGO is None:
        NVIDIA_LOGO = render_logo()
    if HEAVY_LOGO is None:
        HEAVY_LOGO = render_heavy_logo()


def get_nvidia_logo() -> str:
    _lazy_init_logos()
    return NVIDIA_LOGO


def get_heavy_logo() -> str:
    _lazy_init_logos()
    return HEAVY_LOGO