#!/usr/bin/env python3
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
from types import SimpleNamespace

import click
from click._compat import term_len
from click.formatting import HelpFormatter, iter_rows, measure_table, wrap_text

from src.date import Date
from src.dependency import check_dependencies
from src.phockup import Phockup

__version__ = '1.13.0'

PROGRAM_DESCRIPTION = """\
Media sorting tool to organize photos and videos from your camera in folders by year, \
month and day.
The software will collect all files from the input directory and copy them to the output
directory without changing the files content. It will only rename the files and  place
them in the proper directory for year, month and day.
"""

DEFAULT_DIR_FORMAT = ['%Y', '%m', '%d']

logger = logging.getLogger('phockup')


def parse_skip_file_paths_containing(value):
    """Parse a JSON list of path substrings to skip, e.g. '["foo", "bar"]'."""
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "'\"":
        stripped = stripped[1:-1]
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(
            "Invalid format for --skip-file-paths-containing. "
            "Expected a JSON list of strings, e.g. "
            '\'["pattern1", "pattern2"]\''
        ) from exc
    if not isinstance(parsed, list):
        raise click.BadParameter(
            "Invalid format for --skip-file-paths-containing. "
            "Expected a JSON list of strings."
        )
    for item in parsed:
        if not isinstance(item, str):
            raise click.BadParameter(
                "Invalid format for --skip-file-paths-containing. "
                "Each list element must be a string."
            )
    return parsed


def _date_param(value):
    try:
        return Date.parse(value)
    except Exception as exc:
        raise click.BadParameter(str(exc)) from exc


def _regex_param(value):
    try:
        return re.compile(value)
    except re.error as exc:
        raise click.BadParameter(str(exc)) from exc


def _validate_options(options):
    transfer_modes = sum([
        options.move,
        options.link,
        options.rename_in_place,
    ])
    if transfer_modes > 1:
        raise click.UsageError(
            'Only one of --move, --link, or --rename-in-place may be used.'
        )

    output_modes = sum([
        options.debug,
        options.quiet,
        options.progress,
    ])
    if output_modes > 1:
        raise click.UsageError(
            'Only one of --debug, --quiet, or --progress may be used.'
        )


def _options_from_params(**params):
    options = SimpleNamespace(**params)
    _validate_options(options)
    return options


def _terminal_width():
    for stream in (sys.stderr, sys.stdout):
        try:
            if stream.isatty():
                return shutil.get_terminal_size(fileno=stream.fileno()).columns
        except (OSError, ValueError, AttributeError):
            continue
    return shutil.get_terminal_size(fallback=(120, 24)).columns


class _WideHelpFormatter(HelpFormatter):
    """Keep option help on the same line as the option name."""

    def write_dl(self, rows, col_max=30, col_spacing=2):
        rows = list(rows)
        widths = measure_table(rows)
        if len(widths) != 2:
            raise TypeError("Expected two columns for definition list")

        first_col = widths[0] + col_spacing

        for first, second in iter_rows(rows, len(widths)):
            self.write(f"{'':>{self.current_indent}}{first}")
            if not second:
                self.write("\n")
                continue

            padding = max(first_col - term_len(first), col_spacing)
            self.write(" " * padding)

            text_width = max(self.width - self.current_indent - first_col - 2, 10)
            wrapped_text = wrap_text(second, text_width, preserve_paragraphs=True)
            lines = wrapped_text.splitlines()

            if lines:
                self.write(f"{lines[0]}\n")
                for line in lines[1:]:
                    self.write(
                        f"{'':>{first_col + self.current_indent}}{line}\n"
                    )
            else:
                self.write("\n")


class _PhockupContext(click.Context):
    formatter_class = _WideHelpFormatter


class _DynamicWidthCommand(click.Command):
    context_class = _PhockupContext

    def make_context(self, info_name, args, parent=None, **extra):
        width = _terminal_width()
        extra.setdefault('terminal_width', width)
        extra.setdefault('max_content_width', width)
        return super().make_context(info_name, args, parent=parent, **extra)


_HELP_ENTRY_SPACING = '\n\n '


class _SpacedOption(click.Option):
    def get_help_record(self, ctx):
        record = super().get_help_record(ctx)
        if record is None:
            return None
        opts, help_text = record
        if help_text:
            help_text = f'{help_text.rstrip()}{_HELP_ENTRY_SPACING}'
        return opts, help_text


def option(*param_decls, **attrs):
    attrs.setdefault('cls', _SpacedOption)
    return click.option(*param_decls, **attrs)


@click.command(
    cls=_DynamicWidthCommand,
    context_settings={
        'help_option_names': ['-h', '--help'],
    },
    help=PROGRAM_DESCRIPTION,
)
@click.version_option(__version__, '-v', '--version', message='v%(version)s')
@option(
    '-d', '--date',
    type=_date_param,
    help="""\
Specify date format for OUTPUTDIR directories.

You can choose different year format (e.g. 17 instead of 2017) or decide to skip the
day directories and have all photos sorted in year/month.

Supported formats:
    YYYY - 2016, 2017 ...
    YY   - 16, 17 ...
    MM   - 07, 08, 09 ...
    M    - July, August, September ...
    m    - Jul, Aug, Sept ...
    DD   - 27, 28, 29 ... (day of month)
    DDD  - 123, 158, 365 ... (day of year)
    U    - 00, 01, 53 ... (week of the year, Sunday first day of week)
    W    - 00, 01, 53 ... (week of the year, Monday first day of week)

Example:
    YYYY/MM/DD -> 2011/07/17
    YYYY/M/DD  -> 2011/July/17
    YYYY/m/DD  -> 2011/Jul/17
    YY/m-DD    -> 11/Jul-17
    YYYY/U     -> 2011/30
    YYYY/W     -> 2011/28
""",
)
@option(
    '-m', '--move',
    is_flag=True,
    help="""\
Instead of copying the process will move all files from the INPUTDIR to the OUTPUTDIR.
This is useful when working with a big collection of files and the remaining free space
is not enough to make a copy of the INPUTDIR.
""",
)
@option(
    '-l', '--link',
    is_flag=True,
    help="""\
Instead of copying the process will make hard links to all files in INPUTDIR and place
them in the OUTPUTDIR.
This is useful when working with working structure and want to create YYYY/MM/DD
structure to point to same files.
""",
)
@option(
    '--rename-in-place',
    is_flag=True,
    help="""\
Rename files in place without copying or moving them to a new
directory. This is intended for bulk renames when files are
already organized in the correct OUTPUTDIR hierarchy. The tool
will verify that each file's directory matches the expected
year/month/day path and skip any that do not.
""",
)
@option(
    '-o', '--original-names',
    is_flag=True,
    help="""\
Organize the files in selected format or using the default year/month/day format but
keep original filenames.
""",
)
@option(
    '-t', '--timestamp',
    is_flag=True,
    help="""\
Use the timestamp of the file (last modified date) if there is no EXIF date information.
If the user supplies a regex, it will be used if it finds a match in the filename.
This option is intended as "last resort" since the file modified date may not be
accurate, nevertheless it can be useful if no other date information can be obtained.
""",
)
@option(
    '--ctime',
    is_flag=True,
    help="""\
If the date cannot be retrieved from EXIF (or from filename via regex), use the
file's creation time instead. On supported systems this is the birth time; otherwise
the file's ctime (status change time) is used. Ignored if --timestamp is also used
and filename date is not found (--ctime takes precedence over --timestamp in that case).
""",
)
@option(
    '-y', '--dry-run',
    is_flag=True,
    help="""\
Does a trial run with no permanent changes to the filesystem.
So it will not move any files, just shows which changes would be done.
""",
)
@option(
    '-c', '--max-concurrency',
    type=click.IntRange(1, 254),
    default=1,
    show_default=True,
    metavar='1-254',
    help="""\
Sets the level of concurrency for processing files in a directory.
Defaults to 1. Higher values can improve throughput of file operations
""",
)
@option(
    '--maxdepth',
    type=click.IntRange(-1, 254),
    default=-1,
    show_default=True,
    metavar='1-255',
    help="""\
Descend at most 'maxdepth' levels (a non-negative integer) of directories
""",
)
@option(
    '-r', '--regex',
    type=_regex_param,
    help="""\
Specify date format for date extraction from filenames if there is no EXIF date
information.

Example:
    {regex}
    can be used to extract the date from file names like the following
    IMG_27.01.2015-19.20.00.jpg.
""",
)
@option(
    '-f', '--date-field',
    help="""\
Use a custom date extracted from the exif field specified.
To set multiple fields to try in order until finding a valid date, use spaces to
separate fields inside a string.

Example:
    DateTimeOriginal
    "DateTimeOriginal CreateDate FileModifyDate"

These fields are checked by default when this argument is not set:
    "SubSecCreateDate SubSecDateTimeOriginal CreateDate DateTimeOriginal"

To get all date fields available for a file, do:
    exiftool -time:all -mimetype -j <file>
""",
)
@option(
    '--debug',
    is_flag=True,
    default=False,
    help="""\
Enable debugging.  Alternately, set the LOGLEVEL environment variable to DEBUG
""",
)
@option(
    '--quiet',
    is_flag=True,
    default=False,
    help="""\
Run without output.
""",
)
@option(
    '--progress',
    is_flag=True,
    default=False,
    help="""\
Run with progressbar output.
""",
)
@option(
    '--log',
    type=click.Path(),
    help="""\
Specify the output directory where your log file should be exported.
This flag can be used in conjunction with the flag `--quiet` or `--progress`.
""",
)
@click.argument('input_dir', type=click.Path(exists=False))
@click.argument('output_dir', type=click.Path(exists=False))
@option(
    '--file-type',
    type=click.Choice(['image', 'video']),
    help="""\
By default, Phockup addresses both image and video files.
If you want to restrict your command to either images or
videos only, use `--file-type=[image|video]`.
""",
)
@option(
    '--no-date-dir',
    default=Phockup.DEFAULT_NO_DATE_DIRECTORY,
    show_default=True,
    help="""\
Files without EXIF date information are placed in a directory
named 'unknown' by default.  This option overrides that
folder name. e.g. --no-date-dir=misc, --no-date-dir="no date"
""",
)
@option(
    '--skip-unknown',
    is_flag=True,
    default=False,
    help="""\
Ignore files that don't contain valid EXIF data for the criteria specified.
This is useful if you intend to make multiple passes over an input directory
with varying and specific EXIF fields that are note checked by default.
""",
)
@option(
    '--movedel',
    is_flag=True,
    default=False,
    help="""\
DELETE source files which are determined to be duplicates of files
already transferred.  Only valid in conjunction with both `--move`
and `--skip-unknown`.

Also deletes source files whose path contains a pattern matched by
`--skip-file-paths-containing` (including built-in patterns).
""",
)
@option(
    '--skip-file-paths-containing',
    type=parse_skip_file_paths_containing,
    default=None,
    metavar='LIST',
    help="""\
Skip files immediately when their path contains any of the given
substrings, without reading EXIF metadata. Built-in patterns are
always applied (currently: .@__thumb).

Provide a JSON list wrapped in single quotes with each element in
double quotes, for example:
    --skip-file-paths-containing='["@eaDir", ".synology"]'
""",
)
@option(
    '--rmdirs',
    is_flag=True,
    default=False,
    help="""\
DELETE empty directories after processing.  Only valid in
conjunction with `--move`.
""",
)
@option(
    '--output_prefix',
    default='',
    show_default=True,
    help="""\
String to prepend to the output directory to aid in sorting
files by an additional level prior to sorting by date.  This
string will immediately follow the output path and is intended
to allow runtime setting of the output path (e.g. via $USER,
$HOSTNAME, %%USERNAME%%, etc.)
""",
)
@option(
    '--output_suffix',
    default='',
    show_default=True,
    help="""\
String to append to the destination directory to aid in sorting
files by an additional level after sorting by date.
""",
)
@option(
    '--from-date',
    help="""\
Limit the operations to the files that are newer than --from-date (inclusive).
The date must be specified in format YYYY-MM-DD. Files with unknown date won't be skipped.
""",
)
@option(
    '--to-date',
    help="""\
Limit the operations to the files that are older than --to-date (inclusive).
The date must be specified in format YYYY-MM-DD. Files with unknown date won't be skipped.
""",
)
@option(
    '--camera-name-mode',
    type=click.Choice(['prefix', 'suffix']),
    help="""\
If set, include the camera name (from EXIF Make/Model) in the generated
filename. Use:

  --camera-name-mode=prefix  -> <camera>_YYYYMMDD-hhmmss.jpg
  --camera-name-mode=suffix  -> YYYYMMDD-hhmmss_<camera>.jpg

When not set, filenames do not include camera information.
""",
)
@option(
    '--other-dir',
    metavar='DIR',
    help="""\
Collect non-image and non-video files into a dedicated folder under
OUTPUTDIR instead of the default "unknown" directory. Example:
--other-dir=documents
""",
)
@option(
    '--fast-mode',
    is_flag=True,
    default=False,
    help="""\
Reduce per-file logging and progress output for higher throughput on
large collections. Implies no progress bar even if --progress is set.
""",
)
@option(
    '--use-process-pool-for-exif',
    is_flag=True,
    default=False,
    help="""\
Parse EXIF/date metadata in a process pool. This can help when EXIF
extraction is CPU-bound and --max-concurrency is greater than 1.
""",
)
def cli(**params):
    if params['skip_file_paths_containing'] is None:
        params['skip_file_paths_containing'] = []
    options = _options_from_params(**params)
    setup_logging(options)
    return main(options)


def parse_args(args=None):
    """Parse command-line arguments into an options namespace."""
    if args is None:
        args = sys.argv[1:]
    width = _terminal_width()
    ctx = _PhockupContext(
        cli,
        info_name='phockup',
        terminal_width=width,
        max_content_width=width,
    )
    cli.parse_args(ctx, list(args))
    params = dict(ctx.params)
    if params.get('skip_file_paths_containing') is None:
        params['skip_file_paths_containing'] = []
    return _options_from_params(**params)


def setup_logging(options):
    """Configure logging."""
    root = logging.getLogger('')
    root.setLevel(logging.WARNING)
    formatter = logging.Formatter(
        '[%(asctime)s] - [%(levelname)s] - %(message)s', '%Y-%m-%d %H:%M:%S')
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    root.addHandler(ch)
    if not options.quiet ^ options.progress:
        logger.setLevel(options.debug and logging.DEBUG or logging.INFO)
    else:
        logger.setLevel(logging.WARNING)
    if options.log:
        logfile = os.path.expanduser(options.log)
        fh = logging.FileHandler(logfile)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    logger.debug("Debug logging output enabled.")
    logger.debug("Running Phockup version %s", __version__)


def main(options):
    check_dependencies()

    return Phockup(
        options.input_dir,
        options.output_dir,
        dir_format=options.date,
        move=options.move,
        link=options.link,
        date_regex=options.regex,
        original_filenames=options.original_names,
        timestamp=options.timestamp,
        ctime=options.ctime,
        date_field=options.date_field,
        dry_run=options.dry_run,
        quiet=options.quiet,
        progress=options.progress,
        max_depth=options.maxdepth,
        file_type=options.file_type,
        max_concurrency=options.max_concurrency,
        no_date_dir=options.no_date_dir,
        skip_unknown=options.skip_unknown,
        skip_file_paths_containing=options.skip_file_paths_containing,
        movedel=options.movedel,
        rmdirs=options.rmdirs,
        output_prefix=options.output_prefix,
        output_suffix=options.output_suffix,
        from_date=options.from_date,
        to_date=options.to_date,
        rename_in_place=options.rename_in_place,
        camera_name_mode=options.camera_name_mode,
        other_dir=options.other_dir,
        fast_mode=options.fast_mode,
        use_process_pool_for_exif=options.use_process_pool_for_exif,
    )


def run_cli(args=None):
    """Run the CLI with legacy top-level error handling."""
    invoke_kwargs = {'prog_name': 'phockup', 'standalone_mode': False}
    if args is not None:
        invoke_kwargs['args'] = args
    try:
        cli.main(**invoke_kwargs)
    except KeyboardInterrupt:
        logger.error("Exiting phockup...")
        sys.exit(1)
    except click.ClickException as exc:
        logger.warning(exc.format_message())
        sys.exit(exc.exit_code)
    except Exception as exc:
        logger.warning(exc)
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    run_cli()
