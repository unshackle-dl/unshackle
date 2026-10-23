import codecs
import io
import logging
import math
import shutil
import sys
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
from types import ModuleType
from typing import IO, Any, Callable, Iterable, Iterator, List, Literal, Mapping, Optional, TextIO, Union

import rich.cells
from rich import box
from rich._log_render import FormatTimeCallable, LogRender
from rich.cells import cell_len
from rich.color import Color, blend_rgb
from rich.console import (
    Console,
    ConsoleOptions,
    ConsoleRenderable,
    Group,
    HighlighterType,
    RenderableType,
    RenderResult,
)
from rich.control import Control
from rich.emoji import EmojiVariant
from rich.highlighter import Highlighter, ReprHighlighter
from rich.live import Live
from rich.logging import RichHandler
from rich.measure import Measurement
from rich.padding import Padding, PaddingDimensions
from rich.panel import Panel
from rich.progress import BarColumn, Task
from rich.rule import Rule
from rich.segment import ControlType, Segment, SegmentLines, Segments
from rich.status import Status
from rich.style import Style, StyleType
from rich.table import Table
from rich.text import Text, TextType
from rich.theme import Theme

from unshackle.core.config import config
from unshackle.core.themes import apply_help_theme, build_console_theme, load_active_theme


class ComfyLogRenderer(LogRender):
    def __call__(
        self,
        console: "Console",
        renderables: Iterable["ConsoleRenderable"],
        log_time: Optional[datetime] = None,
        time_format: Optional[Union[str, FormatTimeCallable]] = None,
        level: TextType = "",
        path: Optional[str] = None,
        line_no: Optional[int] = None,
        link_path: Optional[str] = None,
    ) -> "Table":
        from rich.containers import Renderables

        output = Table.grid(padding=(0, 5), pad_edge=True)
        output.expand = True
        if self.show_time:
            output.add_column(style="log.time")
        if self.show_level:
            output.add_column(style="log.level", width=self.level_width)
        output.add_column(ratio=1, style="log.message", overflow="fold")
        if self.show_path and path:
            output.add_column(style="log.path")
        row: List["RenderableType"] = []
        if self.show_time:
            log_time = log_time or console.get_datetime()
            time_format = time_format or self.time_format
            if callable(time_format):
                log_time_display = time_format(log_time)
            else:
                log_time_display = Text(log_time.strftime(time_format))
            if log_time_display == self._last_time and self.omit_repeated_times:
                row.append(Text(" " * len(log_time_display)))
            else:
                row.append(log_time_display)
                self._last_time = log_time_display
        if self.show_level:
            row.append(level)

        row.append(Renderables(renderables))
        if self.show_path and path:
            path_text = Text()
            path_text.append(path, style=f"link file://{link_path}" if link_path else "")
            if line_no:
                path_text.append(":")
                path_text.append(
                    f"{line_no}",
                    style=f"link file://{link_path}#{line_no}" if link_path else "",
                )
            row.append(path_text)

        output.add_row(*row)
        return output


class ComfyRichHandler(RichHandler):
    def __init__(
        self,
        level: Union[int, str] = logging.NOTSET,
        console: Optional[Console] = None,
        *,
        show_time: bool = True,
        omit_repeated_times: bool = True,
        show_level: bool = True,
        show_path: bool = True,
        enable_link_path: bool = True,
        highlighter: Optional[Highlighter] = None,
        markup: bool = False,
        rich_tracebacks: bool = False,
        tracebacks_width: Optional[int] = None,
        tracebacks_extra_lines: int = 3,
        tracebacks_theme: Optional[str] = None,
        tracebacks_word_wrap: bool = True,
        tracebacks_show_locals: bool = False,
        tracebacks_suppress: Iterable[Union[str, ModuleType]] = (),
        locals_max_length: int = 10,
        locals_max_string: int = 80,
        log_time_format: Union[str, FormatTimeCallable] = "[%x %X]",
        keywords: Optional[List[str]] = None,
        log_renderer: Optional[LogRender] = None,
    ) -> None:
        super().__init__(
            level=level,
            console=console,
            show_time=show_time,
            omit_repeated_times=omit_repeated_times,
            show_level=show_level,
            show_path=show_path,
            enable_link_path=enable_link_path,
            highlighter=highlighter,
            markup=markup,
            rich_tracebacks=rich_tracebacks,
            tracebacks_width=tracebacks_width,
            tracebacks_extra_lines=tracebacks_extra_lines,
            tracebacks_theme=tracebacks_theme,
            tracebacks_word_wrap=tracebacks_word_wrap,
            tracebacks_show_locals=tracebacks_show_locals,
            tracebacks_suppress=tracebacks_suppress,
            locals_max_length=locals_max_length,
            locals_max_string=locals_max_string,
            log_time_format=log_time_format,
            keywords=keywords,
        )
        if log_renderer:
            self._log_render = log_renderer


class ComfyConsole(Console):
    """A comfy high level console interface.

    Args:
        color_system (str, optional): The colour system supported by your terminal,
            either ``"standard"``, ``"256"`` or ``"truecolor"``. Leave as ``"auto"`` to autodetect.
        force_terminal (Optional[bool], optional): Enable/disable terminal control codes, or None to auto-detect
            terminal. Defaults to None.
        force_jupyter (Optional[bool], optional): Enable/disable Jupyter rendering, or None to auto-detect Jupyter.
            Defaults to None.
        force_interactive (Optional[bool], optional): Enable/disable interactive mode, or None to auto-detect.
            Defaults to None.
        soft_wrap (Optional[bool], optional): Set soft wrap default on print method. Defaults to False.
        theme (Theme, optional): An optional style theme object, or ``None`` for default theme.
        stderr (bool, optional): Use stderr rather than stdout if ``file`` is not specified. Defaults to False.
        file (IO, optional): A file object where the console should write to. Defaults to stdout.
        quiet (bool, Optional): Boolean to suppress all output. Defaults to False.
        width (int, optional): The width of the terminal. Leave as default to auto-detect width.
        height (int, optional): The height of the terminal. Leave as default to auto-detect height.
        style (StyleType, optional): Style to apply to all output, or None for no style. Defaults to None.
        no_color (Optional[bool], optional): Enabled no colour mode, or None to auto-detect. Defaults to None.
        tab_size (int, optional): Number of spaces used to replace a tab character. Defaults to 8.
        record (bool, optional): Boolean to enable recording of terminal output,
            required to call :meth:`export_html`, :meth:`export_svg`, and :meth:`export_text`. Defaults to False.
        markup (bool, optional): Boolean to enable :ref:`console_markup`. Defaults to True.
        emoji (bool, optional): Enable emoji code. Defaults to True.
        emoji_variant (str, optional): Optional emoji variant, either "text" or "emoji". Defaults to None.
        highlight (bool, optional): Enable automatic highlighting. Defaults to True.
        log_time (bool, optional): Boolean to enable logging of time by :meth:`log` methods. Defaults to True.
        log_path (bool, optional): Boolean to enable the logging of the caller by :meth:`log`. Defaults to True.
        log_time_format (Union[str, TimeFormatterCallable], optional): If ``log_time`` is enabled, either string for
            strftime or callable that formats the time. Defaults to "[%X] ".
        highlighter (HighlighterType, optional): Default highlighter.
        legacy_windows (bool, optional): Enable legacy Windows mode, or ``None`` to auto-detect. Defaults to ``None``.
        safe_box (bool, optional): Restrict box options that do not render on legacy Windows.
        get_datetime (Callable[[], datetime], optional): Callable that gets the current time as a datetime.datetime
            object (used by Console.log), or None for datetime.now.
        get_time (Callable[[], time], optional): Callable that gets the current time in seconds, default uses
            time.monotonic.
    """

    def __init__(
        self,
        *,
        color_system: Optional[Literal["auto", "standard", "256", "truecolor", "windows"]] = "auto",
        force_terminal: Optional[bool] = None,
        force_jupyter: Optional[bool] = None,
        force_interactive: Optional[bool] = None,
        soft_wrap: bool = False,
        theme: Optional[Theme] = None,
        stderr: bool = False,
        file: Optional[IO[str]] = None,
        quiet: bool = False,
        width: Optional[int] = None,
        height: Optional[int] = None,
        style: Optional[StyleType] = None,
        no_color: Optional[bool] = None,
        tab_size: int = 8,
        record: bool = False,
        markup: bool = True,
        emoji: bool = True,
        emoji_variant: Optional[EmojiVariant] = None,
        highlight: bool = True,
        log_time: bool = True,
        log_path: bool = True,
        log_time_format: Union[str, FormatTimeCallable] = "[%X]",
        highlighter: Optional["HighlighterType"] = ReprHighlighter(),
        legacy_windows: Optional[bool] = None,
        safe_box: bool = True,
        get_datetime: Optional[Callable[[], datetime]] = None,
        get_time: Optional[Callable[[], float]] = None,
        _environ: Optional[Mapping[str, str]] = None,
        log_renderer: Optional[LogRender] = None,
    ):
        super().__init__(
            color_system=color_system,
            force_terminal=force_terminal,
            force_jupyter=force_jupyter,
            force_interactive=force_interactive,
            soft_wrap=soft_wrap,
            theme=theme,
            stderr=stderr,
            file=file,
            quiet=quiet,
            width=width,
            height=height,
            style=style,
            no_color=no_color,
            tab_size=tab_size,
            record=record,
            markup=markup,
            emoji=emoji,
            emoji_variant=emoji_variant,
            highlight=highlight,
            log_time=log_time,
            log_path=log_path,
            log_time_format=log_time_format,
            highlighter=highlighter,
            legacy_windows=legacy_windows,
            safe_box=safe_box,
            get_datetime=get_datetime,
            get_time=get_time,
            _environ=_environ,
        )
        if log_renderer:
            self._log_render = log_renderer

    def status(
        self,
        status: RenderableType,
        *,
        spinner: str = "dots",
        spinner_style: str = "status.spinner",
        speed: float = 1.0,
        refresh_per_second: float = 12.5,
        pad: PaddingDimensions = (0, 5),
    ) -> Union[Live, Status]:
        """Display a comfy status and spinner.

        Args:
            status (RenderableType): A status renderable (str or Text typically).
            spinner (str, optional): Name of spinner animation (see python -m rich.spinner). Defaults to "dots".
            spinner_style (StyleType, optional): Style of spinner. Defaults to "status.spinner".
            speed (float, optional): Speed factor for spinner animation. Defaults to 1.0.
            refresh_per_second (float, optional): Number of refreshes per second. Defaults to 12.5.
            pad (Union[int, Tuple[int]]): Padding for top, right, bottom, and left borders.
                Give 1, 2, or 4 integers (CSS style).

        Returns:
            Status: A Status object you can use as a context manager.
        """
        status_renderable = super().status(
            status=status,
            spinner=spinner,
            spinner_style=spinner_style,
            speed=speed,
            refresh_per_second=refresh_per_second,
        )

        if pad:
            top, right, bottom, left = Padding.unpack(pad)

            renderable_width = len(status_renderable.status)
            spinner_width = len(status_renderable.renderable.text)
            status_width = spinner_width + renderable_width

            available_width = self.width - status_width
            if available_width > right:
                # fill up the available width with padding to apply bg color
                right = available_width - right

            padding = Padding(status_renderable, (top, right, bottom, left))

            return SyncLive(padding, console=self, transient=True)

        return status_renderable

    def input(
        self,
        prompt: TextType = "",
        *,
        markup: bool = True,
        emoji: bool = True,
        password: bool = False,
        stream: Optional[TextIO] = None,
    ) -> str:
        """Displays a prompt and waits for input from the user, temporarily pausing any active Live displays."""
        # Find active Live displays to prevent overlap / disappearing prompt.
        # We copy the list because stop() will modify console._live_stack.
        active_lives = list(self._live_stack)

        # newest first: Rich's stop() pops the top of the stack
        for live in reversed(active_lives):
            live.stop()

        try:
            return super().input(
                prompt=prompt,
                markup=markup,
                emoji=emoji,
                password=password,
                stream=stream,
            )
        finally:
            for live in active_lives:
                live.start()


active_theme, (primary_scheme, theme_styles, theme_gradients) = load_active_theme(config.theme)
apply_help_theme(primary_scheme)


@contextmanager
def use_gradients(gradients: dict[str, Any]) -> Iterator[None]:
    """Draw with another theme's gradients for the duration, as `env theme NAME` does."""
    global theme_gradients
    saved, theme_gradients = theme_gradients, gradients
    try:
        yield
    finally:
        theme_gradients = saved


@lru_cache(maxsize=32)
def gradient_lut(stops: tuple[str, ...], size: int = 64, cyclic: bool = False) -> list[Style]:
    """Blend colour stops into *size* evenly spaced styles. *cyclic* blends the last stop back into the first."""
    colours = [Color.parse(stop).get_truecolor() for stop in stops]
    if cyclic:
        colours.append(colours[0])
    if len(colours) == 1:
        return [Style(color=Color.from_triplet(colours[0]))]
    lut = []
    for i in range(size):
        t = i / (size - 1) * (len(colours) - 1)
        n = min(int(t), len(colours) - 2)
        lut.append(Style(color=Color.from_triplet(blend_rgb(colours[n], colours[n + 1], t - n))))
    return lut


class Gradient:
    """Draw *renderable*, then recolour the cells drawn in *style*'s colour along the theme's *surface* gradient.

    With *frame_only*, only the box-drawing characters on the outer edge change, so a panel's title and the lines inside
    keep their colours, even a tree guide drawn in the border's colour. Each recoloured cell carries its place along the
    gradient in its style's meta, so `slide_gradients` can animate the drawn lines.
    """

    def __init__(
        self,
        renderable: RenderableType,
        surface: str,
        style: StyleType,
        frame_only: bool = False,
        direction: Optional[str] = None,
    ) -> None:
        self.renderable = renderable
        self.surface = surface
        self.style = style
        self.frame_only = frame_only
        self.direction = direction

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        stops = theme_gradients.get(self.surface)
        if not stops:
            yield self.renderable
            return
        colour = console.get_style(self.style).color
        lines = console.render_lines(self.renderable, options, pad=False, new_lines=True)

        def candidate(segment: Segment) -> bool:
            return not segment.control and segment.style is not None and segment.style.color == colour

        def characters(line: list[Segment]) -> Iterator[tuple[int, Segment, str]]:
            x = 0
            for segment in line:
                for character in segment.text if candidate(segment) else [segment.text]:
                    yield x, segment, character
                    x += cell_len(character)

        marks = [
            (x, y, character)
            for y, line in enumerate(lines)
            for x, segment, character in characters(line)
            if candidate(segment) and character.strip()
        ]
        cells = {(x, y) for x, y, _ in marks}
        if self.frame_only:
            boxes = [(x, y) for x, y, character in marks if "─" <= character <= "╿"]
            ends = (min(y for _, y in boxes), max(y for _, y in boxes)) if boxes else ()
            left: dict[int, int] = {}
            right: dict[int, int] = {}
            for x, y in boxes:
                left[y], right[y] = min(left.get(y, x), x), max(right.get(y, x), x)
            cells = {(x, y) for x, y in boxes if y in ends or x in (left[y], right[y])}
        if not cells:
            yield from (segment for line in lines for segment in line)
            return
        x0, y0 = min(x for x, _ in cells), min(y for _, y in cells)
        direction = self.direction or theme_gradients.get("direction", "horizontal")

        def distance(x: int, y: int) -> int:
            return {"horizontal": x - x0, "vertical": y - y0}.get(direction, x - x0 + 2 * (y - y0))

        span = max(distance(x, y) for x, y in cells) or 1
        lut = gradient_lut(stops)

        def recoloured() -> Iterator[Segment]:
            for y, line in enumerate(lines):
                for x, segment, character in characters(line):
                    if candidate(segment) and (x, y) in cells:
                        t = distance(x, y) / span
                        tint = lut[round(t * (len(lut) - 1))] + Style(meta={"gradient": (self.surface, t)})
                        yield Segment(character, (segment.style or Style.null()) + tint)
                    elif candidate(segment):
                        yield Segment(character, segment.style)
                    else:
                        yield segment

        yield from Segment.simplify(recoloured())

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement.get(console, options, self.renderable)


def slide_gradients(lines: list[list[Segment]], offset: float) -> list[list[Segment]]:
    """Slide the gradient colours that `Gradient` drew into *lines* along by *offset* gradient lengths, looping the last
    stop back into the first, so a sequence of offsets animates them. Other cells keep their colours."""
    return [[_slid(segment, offset) for segment in line] for line in lines]


def _slid(segment: Segment, offset: float) -> Segment:
    mark = _gradient_mark(segment.style) if segment.style else None
    stops = theme_gradients.get(mark[0]) if mark else None
    if not mark or not stops or segment.style is None:
        return segment
    lut = gradient_lut(stops, cyclic=True)
    return Segment(segment.text, segment.style + lut[round((mark[1] - offset) % 1 * (len(lut) - 1))])


@lru_cache(maxsize=4096)
def _gradient_mark(style: Style) -> Optional[tuple[str, float]]:
    """The surface and the place along it of a cell that `Gradient` coloured, or None."""
    return style.meta.get("gradient") if style._meta else None


class _Drawn:
    """A renderable that draws *target* with Rich's own, unpatched *render* method."""

    def __init__(self, render: Callable[..., RenderResult], target: Any) -> None:
        self.render = render
        self.target = target

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        return self.render(self.target, console, options)


def _paint_with(
    render: Callable[..., RenderResult], surface: str, style_attribute: str, direction: Optional[str] = None
) -> Callable[..., RenderResult]:
    """Wrap a Rich class's render method so the theme's *surface* gradient colours its lines."""

    def __rich_console__(self: Any, console: Console, options: ConsoleOptions) -> RenderResult:
        if not theme_gradients.get(surface):
            return render(self, console, options)
        gradient = Gradient(_Drawn(render, self), surface, getattr(self, style_attribute), True, direction)
        return gradient.__rich_console__(console, options)

    return __rich_console__


Rule.__rich_console__ = _paint_with(Rule.__rich_console__, "rule", "style", "horizontal")  # type: ignore[method-assign]
Panel.__rich_console__ = _paint_with(Panel.__rich_console__, "panel", "border_style")  # type: ignore[method-assign]

_SYNC_BEGIN = Segments([Segment("\x1b[?2026h", None, [(ControlType.BELL,)])])
_SYNC_END = Segments([Segment("\x1b[?2026l", None, [(ControlType.BELL,)])])


class SyncLive(Live):
    """Live that wraps each repaint, and each print while it runs, in DEC 2026 begin/end-sync, so the terminal draws
    the frame as one."""

    def process_renderables(self, renderables: list[ConsoleRenderable]) -> list[ConsoleRenderable]:
        return self.synced(super().process_renderables(renderables))

    def synced(self, renderables: list[ConsoleRenderable]) -> list[ConsoleRenderable]:
        if not self.console.is_interactive or self.console.legacy_windows:
            return renderables
        return [_SYNC_BEGIN, *renderables, _SYNC_END]


class PinnedLive(SyncLive):
    """A root live area that holds the screen for a whole command: *header* at the top, the newest output under it,
    and the nested live areas (spinners, progress bars) at the bottom.

    It keeps what is printed while it runs, and sends the oldest lines to the scrollback above the live area once the
    screen is full, so they scroll off the top. The header goes to the scrollback first, so the scrollback ends up as a
    plain run would leave it. On screen, the gradients in the header and the kept lines move.
    """

    LOOP_SECONDS = 4.0

    def __init__(self, header: RenderableType, live_console: Console) -> None:
        super().__init__(console=live_console, transient=True, refresh_per_second=20)
        self.header = header
        self.header_lines: tuple[int, list[list[Segment]]] = (-1, [])
        self.kept: list[list[Segment]] = []
        self.room = live_console.size.height
        self.header_sent = False
        self.started_at = time.monotonic()

    @staticmethod
    def wanted(live_console: Console) -> bool:
        """Whether the terminal can show the moving header: interactive, with colour, and wide enough."""
        colour = live_console.color_system is not None and not live_console.no_color
        return live_console.is_interactive and colour and fits_terminal(live_console)

    @property
    def renderable(self) -> RenderableType:
        return _PinnedScreen(self)

    def process_renderables(self, renderables: list[ConsoleRenderable]) -> list[ConsoleRenderable]:
        if not self.console.is_interactive:
            return super().process_renderables(renderables)
        with self._lock:
            printed = [renderable for renderable in renderables if not isinstance(renderable, Control)]
            capture = [_Capture(self, printed)] if printed else []
            return self.synced([self._live_render.position_cursor(), *capture, self._live_render])

    def head(self, options: ConsoleOptions) -> list[list[Segment]]:
        """The header's lines, drawn once for each width."""
        width, lines = self.header_lines
        if width != options.max_width:
            lines = self.console.render_lines(self.header, options, pad=False)
            self.header_lines = (options.max_width, lines)
        return lines

    def overflow(self) -> list[list[Segment]]:
        """Take off the oldest kept lines that no longer fit on the screen. Call it with the lock held."""
        count = max(0, len(self.kept) - self.room)
        out = self.kept[:count]
        del self.kept[:count]
        return out

    def still(self, lines: list[list[Segment]]) -> list[RenderableType]:
        """*lines* for the scrollback, after the header if it has not gone there yet."""
        out: list[RenderableType] = [] if self.header_sent else [self.header]
        self.header_sent = True
        return [*out, SegmentLines(lines, new_lines=True)]

    def close(self) -> None:
        """Stop the live area, then print what is still kept."""
        self.stop()
        with self._lock:
            kept, self.kept = self.kept, []
            for renderable in self.still(kept):
                self.console.print(renderable, crop=False)


class _Capture:
    """The output of one print while PinnedLive runs. Rendered with the print's own options, it keeps the lines in
    PinnedLive and yields only the oldest kept lines that no longer fit, which land above the live area."""

    def __init__(self, live: PinnedLive, renderables: list[ConsoleRenderable]) -> None:
        self.live = live
        self.renderables = renderables

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        lines = [
            line for renderable in self.renderables for line in console.render_lines(renderable, options, pad=False)
        ]
        with self.live._lock:
            self.live.kept.extend(lines)
            flushed = self.live.overflow()
            if flushed:
                yield from self.live.still(flushed)


class _PinnedScreen:
    def __init__(self, live: PinnedLive) -> None:
        self.live = live

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        offset = (time.monotonic() - self.live.started_at) / self.live.LOOP_SECONDS
        head = self.live.head(options)
        nested = [live.get_renderable() for live in console._live_stack[1:]]
        foot = console.render_lines(Group(*nested), options, pad=False) if nested else []
        room = self.live.room = max(0, console.size.height - len(head) - len(foot))
        body = self.live.kept[-room:] if room else []
        for line in slide_gradients([*head, *body], offset) + foot:
            yield from Segment.adjust_line_length(line, options.max_width, pad=False)
            yield Segment.line()


PULSE_PERIOD = 40.0
PULSE_SPEED = 15.0  # cells a second


def fits_terminal(live_console: Console) -> bool:
    """Whether the terminal is at least as wide as *live_console*. A live area that wraps leaves old frames behind,
    because Rich moves the cursor up by the lines it drew, not by the rows the terminal used."""
    return shutil.get_terminal_size((live_console.width, 0)).columns >= live_console.width


def bar_gradients(palette: Mapping[str, str]) -> tuple[bool, Optional[list[Style]], list[Style]]:
    """The progress bar colours of the gradients in use: whether they scroll, the fill (None without a `bar`
    gradient) and the pulse, which without a `pulse` gradient blends *palette*'s dark_gray into its pink."""
    scroll = theme_gradients.get("motion") == "scroll"
    bar = theme_gradients.get("bar")
    pulse = gradient_lut(theme_gradients.get("pulse") or (palette["dark_gray"], palette["pink"]))
    return scroll, gradient_lut(bar, cyclic=scroll) if bar else None, pulse


class GradientPulseBarColumn(BarColumn):
    def __init__(
        self, *args: Any, pulse_period: float = PULSE_PERIOD, pulse_speed: float = PULSE_SPEED, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.pulse_period = pulse_period
        self.pulse_speed = pulse_speed
        self.scroll, self._fill_lut, self._lut = bar_gradients(primary_scheme)

    def render(self, task: Task) -> RenderableType:  # type: ignore[override]
        if task.started and task.total is not None:
            if self._fill_lut is None or task.completed >= task.total:
                return super().render(task)
            fraction = min(1.0, max(0.0, task.completed / task.total)) if task.total else 1.0
            offset = task.get_time() * self.pulse_speed if self.scroll else None
            return _GradientFill(self._fill_lut, fraction, offset, self.bar_width, self.style)
        return _GradientPulse(self._lut, task.get_time() * self.pulse_speed, self.pulse_period, self.bar_width)


def gradient_fill(
    lut: list[Style],
    fraction: float,
    width: int,
    offset: Optional[float],
    back: Optional[StyleType],
    ascii: bool = False,
) -> Text:
    """Draw a progress bar *fraction* full, with the gradient fixed along the bar or, given an *offset*, slid along it."""
    full, half = divmod(int(width * 2 * fraction), 2)
    bar = Text()
    for i in range(full + half):
        t = i / max(1, width - 1) if offset is None else (i - offset) / width % 1
        character = ("-" if ascii else "━") if i < full else (" " if ascii else "╸")
        bar.append(character, lut[round(t * (len(lut) - 1))])
    remaining = width - full - half
    if remaining and back is not None:
        if not half and full:
            bar.append(" " if ascii else "╺", back)
            remaining -= 1
        bar.append(("-" if ascii else "━") * remaining, back)
    return bar


class _GradientFill:
    def __init__(
        self, lut: list[Style], fraction: float, offset: Optional[float], width: Optional[int], back: StyleType
    ) -> None:
        self.lut = lut
        self.fraction = fraction
        self.offset = offset
        self.width = width
        self.back = back

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = min(self.width or options.max_width, options.max_width)
        # as ProgressBar: without colour the empty part would look full
        back = self.back if not console.no_color and console.color_system is not None else None
        yield gradient_fill(
            self.lut, self.fraction, width, self.offset, back, options.legacy_windows or options.ascii_only
        )

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(self.width, self.width) if self.width is not None else Measurement(4, options.max_width)


class _GradientPulse:
    def __init__(self, lut: list[Style], offset: float, period: float, width: Optional[int]) -> None:
        self.lut = lut
        self.offset = offset
        self.period = period
        self.width = width

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = min(self.width or options.max_width, options.max_width)
        bar = Text()
        for i in range(width):
            fade = (math.cos((i - self.offset) * math.tau / self.period) + 1) / 2
            bar.append("━", self.lut[int(fade * (len(self.lut) - 1))])
        yield bar

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(4, self.width or options.max_width)


def force_utf8_streams() -> None:
    """Make stdout and stderr write UTF-8. A redirected stream on Windows uses a legacy code page that cannot encode the console output."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and codecs.lookup(stream.encoding).name != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")


force_utf8_streams()

_rich_cell_size = rich.cells.get_character_cell_size


@lru_cache(maxsize=4096)
def _cell_size(character: str, *args: str) -> int:
    """Count a spacing combining mark (Mc, e.g. Devanagari ि) as one cell, as terminals draw it; Rich's table says 0."""
    return 1 if unicodedata.category(character) == "Mc" else _rich_cell_size(character, *args)


rich.cells.get_character_cell_size = _cell_size
rich.cells.cached_cell_len.cache_clear()

console = ComfyConsole(
    log_time=False,
    log_path=False,
    width=80,
    theme=build_console_theme(primary_scheme, theme_styles, config.set_terminal_bg),
    log_renderer=ComfyLogRenderer(show_time=False, show_path=False),
)


def listing_panel(renderable: RenderableType, title: str) -> Panel:
    """Box a listing in the shared panel style, so every listing restyles from one place."""
    return Panel(renderable, title=title, box=box.SQUARE, border_style="bright_black")


def listing_table(title: Optional[str] = None, **kwargs: Any) -> Table:
    """Assemble a table in the style of the rich-click help tables, so listings and help screens match."""
    kwargs.setdefault("box", box.HORIZONTALS)
    kwargs.setdefault("show_edge", False)  # no top or bottom rule, so the title runs straight into the header
    kwargs.setdefault("border_style", "dark_gray")
    kwargs.setdefault("header_style", "text")
    kwargs.setdefault("pad_edge", False)
    return Table(title=title, title_style="rule.text", title_justify="left", **kwargs)


def print_wide(renderable: RenderableType, pad: PaddingDimensions = (1, 3)) -> None:
    """Print a listing at the terminal width, indented like the rich-click help panels.

    The shared console holds 80 columns to keep logs and progress compact. Long values such as paths
    and URLs fold badly at that width, so this widens the console for one print and restores it after.
    """
    fixed_width = console.width
    console.width = max(fixed_width, shutil.get_terminal_size().columns)
    try:
        console.print(Padding(renderable, pad))
    finally:
        console.width = fixed_width


_prompt_handler: Optional[Callable[[str], str]] = None


def set_prompt_handler(handler: Optional[Callable[[str], str]]) -> None:
    """Route every :func:`prompt_user` call to *handler*, for a process with no terminal to prompt on."""
    global _prompt_handler
    _prompt_handler = handler


def prompt_user(prompt: str) -> str:
    """Ask the user for input on the shared console, themed and indented like the rest of the output."""
    if _prompt_handler is not None:
        return _prompt_handler(prompt)
    indent = " " * 5
    body = Text(indent + prompt.rstrip("\n ").replace("\n", "\n" + indent), style="text")
    body.append("\n" + indent + "> ", style="rule.text")
    return console.input(body)


_notice_handler: Optional[Callable[[Optional[str]], None]] = None


def set_notice_handler(handler: Optional[Callable[[Optional[str]], None]]) -> None:
    """Route every :func:`show_notice` call to *handler*, for a process whose log output no user reads."""
    global _notice_handler
    _notice_handler = handler


def show_notice(message: Optional[str]) -> None:
    """Give *message* to the notice handler, or None to clear it. Without a handler this does nothing."""
    if _notice_handler is not None:
        _notice_handler(message)


__all__ = (
    "ComfyLogRenderer",
    "ComfyRichHandler",
    "ComfyConsole",
    "Gradient",
    "GradientPulseBarColumn",
    "PinnedLive",
    "SyncLive",
    "console",
    "listing_panel",
    "listing_table",
    "print_wide",
    "prompt_user",
    "set_notice_handler",
    "set_prompt_handler",
    "show_notice",
    "use_gradients",
)
