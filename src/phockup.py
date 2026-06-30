#!/usr/bin/env python3
import concurrent.futures
import filecmp
import logging
import os
import re
import shutil
import sys
import threading
import time

from tqdm import tqdm

from src.date import Date
from src.exif import Exif

logger = logging.getLogger('phockup')
ignored_files = ('.DS_Store', 'Thumbs.db')
DEFAULT_SKIP_FILE_PATH_PATTERNS = ('.@__thumb',)

# Known file extensions for basic pre-filtering
IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.jpe', '.jfif', '.png', '.gif', '.bmp', '.tif', '.tiff',
    '.webp', '.heic', '.heif', '.raw', '.cr2', '.cr3', '.nef', '.arw',
    '.orf', '.rw2', '.dng', '.psd'
}

VIDEO_EXTENSIONS = {
    '.mp4', '.m4v', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm', '.mts',
    '.m2ts', '.3gp', '.3g2'
}

_START_OF_DAY = '00:00:00'
_END_OF_DAY = '23:59:59'
_DATETIME_PARSE_FMT = '%Y-%m-%d %H:%M:%S'


def _extract_exif_and_date(filename, timestamp, date_regex, date_field, ctime=False):
    """
    Helper function to extract exif data and date information.
    This is defined at module level so it can be used with ProcessPoolExecutor
    if needed without pickling bound methods.
    """
    exif_data = Exif(filename).data()
    target_file_type = None

    if exif_data and 'MIMEType' in exif_data:
        patternImage = re.compile('^(image/.+|application/vnd.adobe.photoshop)$')
        patternVideo = re.compile('^(video/.*)$')
        if patternImage.match(exif_data['MIMEType']):
            target_file_type = 'image'
        elif patternVideo.match(exif_data['MIMEType']):
            target_file_type = 'video'

    date = None
    if target_file_type in ['image', 'video']:
        date = Date(filename).from_exif(
            exif_data, timestamp, date_regex, date_field, ctime=ctime
        )

    return exif_data, target_file_type, date


class Phockup:
    DEFAULT_DIR_FORMAT = ['%Y', '%m', '%d']
    DEFAULT_NO_DATE_DIRECTORY = "unknown"

    def __init__(self, input_dir, output_dir, **args):
        start_time = time.time()
        self.files_processed = 0
        self.duplicates_found = 0
        self.unknown_found = 0
        self.files_moved = 0
        self.files_copied = 0

        input_dir = os.path.expanduser(input_dir)
        output_dir = os.path.expanduser(output_dir)

        if input_dir.endswith(os.path.sep):
            input_dir = input_dir[:-1]
        if output_dir.endswith(os.path.sep):
            output_dir = output_dir[:-1]

        self.input_dir = input_dir
        self.output_dir = output_dir
        self.output_prefix = args.get('output_prefix' or None)
        self.output_suffix = args.get('output_suffix' or '')
        self.no_date_dir = args.get('no_date_dir') or Phockup.DEFAULT_NO_DATE_DIRECTORY
        self.dir_format = args.get('dir_format') or os.path.sep.join(Phockup.DEFAULT_DIR_FORMAT)
        self.move = args.get('move', False)
        self.link = args.get('link', False)
        self.original_filenames = args.get('original_filenames', False)
        self.date_regex = args.get('date_regex', None)
        self.timestamp = args.get('timestamp', False)
        self.ctime = args.get('ctime', False)
        self.date_field = args.get('date_field', False)
        self.skip_unknown = args.get("skip_unknown", False)
        self.movedel = args.get("movedel", False)
        user_skip_path_patterns = args.get('skip_file_paths_containing') or ()
        self.skip_file_path_patterns = DEFAULT_SKIP_FILE_PATH_PATTERNS + tuple(
            user_skip_path_patterns
        )
        self.rmdirs = args.get("rmdirs", False)
        self.dry_run = args.get('dry_run', False)
        self.progress = args.get('progress', False)
        self.rename_in_place = args.get("rename_in_place", False)
        self.fast_mode = args.get('fast_mode', False)
        self.max_depth = args.get('max_depth', -1)
        # default to concurrency of one to retain existing behavior
        self.max_concurrency = args.get("max_concurrency", 1)
        self._file_lock = threading.Lock()
        # Optional process pool toggle for EXIF/date extraction when CPU bound
        self.use_process_pool_for_exif = args.get("use_process_pool_for_exif", False)
        # Optional directory where non-image / non-video files are collected
        self.other_dir = args.get("other_dir", None)
        # Optional camera name placement: 'prefix', 'suffix' or None
        self.camera_name_mode = args.get("camera_name_mode", None)
        self._created_dirs = set()

        if self.fast_mode:
            # In fast mode, disable per-file progress bar to reduce overhead
            self.progress = False

        self.from_date = args.get("from_date", None)
        self.to_date = args.get("to_date", None)
        if self.from_date is not None:
            self.from_date = Date.strptime(
                '{} {}'.format(self.from_date, _START_OF_DAY),
                _DATETIME_PARSE_FMT,
            )
        if self.to_date is not None:
            self.to_date = Date.strptime(
                '{} {}'.format(self.to_date, _END_OF_DAY),
                _DATETIME_PARSE_FMT,
            )

        if self.max_concurrency > 1:
            logger.info(f"Using {self.max_concurrency} workers to process files.")

        self.stop_depth = self.input_dir.count(os.sep) + self.max_depth \
            if self.max_depth > -1 else sys.maxsize
        self.file_type = args.get('file_type', None)

        if self.dry_run:
            logger.warning("Dry-run phockup (does a trial run with no permanent changes)...")

        self.check_directories()
        # Get the number of files
        if self.progress:
            file_count = self.get_file_count()
            with tqdm(desc=f"Progressing: '{self.input_dir}' ",
                      total=file_count,
                      unit="file",
                      position=0,
                      leave=True,
                      ascii=(sys.platform == 'win32')) as self.pbar:
                self.walk_directory()
        else:
            self.pbar = None
            self.walk_directory()

        if self.move and self.rmdirs:
            self.rm_subdirs()

        run_time = time.time() - start_time
        if self.files_processed and run_time:
            self.print_action_report(run_time)

    def print_action_report(self, run_time):
        throughput = self.files_processed / run_time
        logger.info(
            'Processed %d files in %.2f seconds. '
            'Average Throughput: %.2f files/second'
            % (self.files_processed, run_time, throughput)
        )
        if self.unknown_found:
            logger.info(f"Found {self.unknown_found} files without EXIF date data.")
        if self.duplicates_found:
            logger.info(f"Found {self.duplicates_found} duplicate files.")
        if self.files_copied:
            if self.dry_run:
                logger.info(f"Would have copied {self.files_copied} files.")
            else:
                logger.info(f"Copied {self.files_copied} files.")
        if self.files_moved:
            if self.dry_run:
                logger.info(f"Would have moved {self.files_moved} files.")
            else:
                logger.info(f"Moved {self.files_moved} files.")

    def check_directories(self):
        """
        Check if input and output directories exist.
        If input does not exist it exits the process.
        If output does not exist it tries to create it or exit with error.
        """

        if not os.path.exists(self.input_dir):
            raise RuntimeError(f"Input directory '{self.input_dir}' does not exist")
        if not os.path.isdir(self.input_dir):
            raise RuntimeError(f"Input directory '{self.input_dir}' is not a directory")
        if not os.path.exists(self.output_dir):
            logger.warning(f"Output directory '{self.output_dir}' does not exist, creating now")
            try:
                if not self.dry_run:
                    os.makedirs(self.output_dir)
            except OSError:
                raise OSError(f"Cannot create output '{self.output_dir}' directory. No write access!")

    def skip_file_path_match(self, path):
        """Return the first skip pattern found in path, or None."""
        for pattern in self.skip_file_path_patterns:
            if pattern in path:
                return pattern
        return None

    def handle_skip_file_path(self, full_path, matched_pattern):
        """Skip or delete a file whose path contains a skip pattern."""
        if self.movedel:
            if not self.dry_run:
                os.remove(full_path)
            progress = (f"{full_path} => deleted, path contains "
                        f"'{matched_pattern}'")
        else:
            progress = (f"{full_path} => skipped, path contains "
                        f"'{matched_pattern}'")

        if self.progress:
            self.pbar.write(progress)
        if not self.fast_mode:
            logger.info(progress)

    def walk_directory(self):
        """
        Walk input directory recursively and call process_file for each file
        except the ignored ones.
        """

        # Walk the directory
        for root, dirnames, files in os.walk(self.input_dir):
            files.sort()
            file_paths_to_process = []

            # Basic extension-based pre-filtering to avoid unnecessary EXIF work.
            # This only applies when the user requested a specific file_type;
            # otherwise we retain existing behavior and process all files.
            want_images = self.file_type == 'image'
            want_videos = self.file_type == 'video'

            for filename in files:
                if filename in ignored_files:
                    continue

                full_path = os.path.join(root, filename)

                matched_pattern = self.skip_file_path_match(full_path)
                if matched_pattern is not None:
                    self.handle_skip_file_path(full_path, matched_pattern)
                    continue

                if want_images or want_videos:
                    ext = os.path.splitext(filename)[1].lower()
                    if want_images and ext not in IMAGE_EXTENSIONS:
                        continue
                    if want_videos and ext not in VIDEO_EXTENSIONS:
                        continue

                file_paths_to_process.append(full_path)

            if self.max_concurrency > 1:
                if not self.process_files(file_paths_to_process):
                    return
            else:
                try:
                    for file_path in file_paths_to_process:
                        self.process_file(file_path)
                except KeyboardInterrupt:
                    logger.warning("Received interrupt. Shutting down...")
                    return
            if root.count(os.sep) >= self.stop_depth:
                del dirnames[:]

    def rm_subdirs(self):
        def _get_depth(sub_path):
            return sub_path.count(os.sep) - self.input_dir.count(os.sep)

        for root, dirs, files in os.walk(self.input_dir, topdown=False):
            # Traverse the tree bottom-up
            if _get_depth(root) > self.stop_depth:
                continue
            for name in dirs:
                dir_path = os.path.join(root, name)
                if _get_depth(dir_path) > self.stop_depth:
                    continue
                try:
                    os.rmdir(dir_path)  # Try to remove the dir
                    logger.info(f"Deleted empty directory: {dir_path}")
                except OSError as e:
                    logger.info(f"{e.strerror} - {dir_path} not deleted.")

    def get_file_count(self):
        file_count = 0
        for root, dirnames, files in os.walk(self.input_dir):
            file_count += len(files)
            if root.count(os.sep) >= self.stop_depth:
                del dirnames[:]
        return file_count

    def get_file_type(self, mimetype):
        """
        Check if given file_type is image or video
        Return None if other
        Use mimetype to determine if the file is an image or video.
        """
        patternImage = re.compile('^(image/.+|application/vnd.adobe.photoshop)$')
        if patternImage.match(mimetype):
            return 'image'

        patternVideo = re.compile('^(video/.*)$')
        if patternVideo.match(mimetype):
            return 'video'
        return None

    def get_output_dir(self, date):
        """
        Generate output directory path based on the extracted date and
        formatted using dir_format.
        If date is missing from the exifdata the file is going to "unknown"
        directory unless user included a regex from filename or uses timestamp.
        """
        try:
            path = [self.output_dir,
                    self.output_prefix,
                    date['date'].date().strftime(self.dir_format),
                    self.output_suffix]
        except (TypeError, ValueError):
            path = [self.output_dir,
                    self.output_prefix,
                    self.no_date_dir,
                    self.output_suffix]
        # Remove any None values that made it in the path
        path = [p for p in path if p is not None]
        fullpath = os.path.normpath(os.path.sep.join(path))

        if fullpath not in self._created_dirs and not self.dry_run:
            if not os.path.isdir(fullpath):
                os.makedirs(fullpath, exist_ok=True)
            self._created_dirs.add(fullpath)

        return fullpath

    def get_other_dir(self):
        """
        Generate output directory path for non-image / non-video files.
        If other_dir is not specified, fall back to the "unknown" directory
        behavior used for files without EXIF date data.
        """
        if self.other_dir:
            path = [self.output_dir,
                    self.output_prefix,
                    self.other_dir,
                    self.output_suffix]
        else:
            path = [self.output_dir,
                    self.output_prefix,
                    self.no_date_dir,
                    self.output_suffix]

        # Remove any None values that made it in the path
        path = [p for p in path if p is not None]
        fullpath = os.path.normpath(os.path.sep.join(path))

        if fullpath not in self._created_dirs and not self.dry_run:
            if not os.path.isdir(fullpath):
                os.makedirs(fullpath, exist_ok=True)
            self._created_dirs.add(fullpath)

        return fullpath

    def get_file_name(self, original_filename, date, camera_name=None):
        """
        Generate file name based on exif data unless it is missing or
        original filenames are required. Then use original file name
        """
        if self.original_filenames:
            return os.path.basename(original_filename)

        try:
            filename = [
                f'{date["date"].year :04d}',
                f'{date["date"].month :02d}',
                f'{date["date"].day :02d}',
                '-',
                f'{date["date"].hour :02d}',
                f'{date["date"].minute :02d}',
                f'{date["date"].second :02d}',
            ]

            if date['subseconds']:
                filename.append(date['subseconds'])

            base_name = ''.join(filename)

            if camera_name and self.camera_name_mode in ("prefix", "suffix"):
                if self.camera_name_mode == "prefix":
                    base_name = f"{camera_name}_{base_name}"
                else:
                    base_name = f"{base_name}_{camera_name}"

            return base_name + os.path.splitext(original_filename)[1]
        # TODO: Double check if this is correct!
        except TypeError:
            return os.path.basename(original_filename)

    def process_files(self, file_paths_to_process):
        # With all the appropriate files in the directory added to the
        # list, process the directory concurrently using threads
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_concurrency) as executor:
            try:
                for _ in executor.map(self.process_file,
                                      file_paths_to_process):
                    pass
            except KeyboardInterrupt:
                logger.warning(
                        f"Received interrupt. Shutting down {self.max_concurrency} workers...")
                executor.shutdown(wait=True)
                return False
        return True

    def process_file(self, filename):
        """
        Process the file using the selected strategy
        If file is .xmp skip it so process_xmp method can handle it
        """
        if str.endswith(filename, '.xmp'):
            return None

        progress = f'{filename}'

        output, target_file_name, target_file_path, target_file_type, file_date = self.get_file_name_and_path(filename)
        suffix = 1
        target_file = target_file_path

        while True:
            if self.file_type is not None \
                    and self.file_type != target_file_type:
                progress = f"{progress} => skipped, file is '{target_file_type}' but looking for '{self.file_type}'"
                if not self.fast_mode:
                    logger.info(progress)
                break

            date_unknown = file_date is None or output.endswith(self.no_date_dir)
            if self.skip_unknown and output.endswith(self.no_date_dir):
                # Skip files that didn't generate a path from EXIF data
                progress = f"{progress} => skipped, unknown date EXIF information for '{target_file_name}'"
                self.unknown_found += 1
                if self.progress:
                    self.pbar.write(progress)
                logger.info(progress)
                break

            if not date_unknown:
                skip = False
                if type(file_date) is dict:
                    file_date = file_date["date"]
                if self.from_date is not None and file_date < self.from_date:
                    progress = f"{progress} => {filename} skipped: date {file_date} is older than --from-date {self.from_date}"
                    skip = True
                if self.to_date is not None and file_date > self.to_date:
                    progress = f"{progress} => {filename} skipped: date {file_date} is newer than --to-date {self.to_date}"
                    skip = True
                if skip:
                    if self.progress:
                        self.pbar.write(progress)
                    if not self.fast_mode:
                        logger.info(progress)
                    break

            # In rename-in-place mode, verify that the file already resides in
            # the expected output hierarchy. This check is O(1) per file and
            # reuses the already computed output path.
            if self.rename_in_place:
                current_dir = os.path.normpath(os.path.dirname(filename))
                expected_dir = os.path.normpath(output)
                if current_dir != expected_dir:
                    progress = (f"{progress} => skipped, directory hierarchy mismatch "
                                f"(expected '{expected_dir}', found '{current_dir}')")
                    if self.progress:
                        self.pbar.write(progress)
                    logger.info(progress)
                    break

            with self._file_lock:
                if os.path.isfile(target_file):
                    # Duplicate detection: first use a quick size comparison to
                    # rule out obvious non-duplicates, then fall back to a full
                    # byte-for-byte comparison to retain original behavior.
                    is_duplicate = False
                    if filename != target_file:
                        try:
                            if os.path.getsize(filename) == os.path.getsize(target_file):
                                is_duplicate = filecmp.cmp(
                                    filename,
                                    target_file,
                                    shallow=False,
                                )
                        except OSError:
                            is_duplicate = False

                    if is_duplicate:
                        if self.movedel and self.move and self.skip_unknown:
                            if not self.dry_run:
                                os.remove(filename)
                            progress = f'{progress} => deleted, duplicated file {target_file}'
                        else:
                            progress = f'{progress} => skipped, duplicated file {target_file}'
                        self.duplicates_found += 1
                        if self.progress:
                            self.pbar.write(progress)
                        if not self.fast_mode:
                            logger.info(progress)
                        break
                else:
                    if self.rename_in_place:
                        try:
                            # Treat in-place renames as "moves" for reporting
                            self.files_moved += 1
                            if not self.dry_run and filename != target_file:
                                os.rename(filename, target_file)
                        except FileNotFoundError:
                            progress = f'{progress} => skipped, no such file or directory'
                            if self.progress:
                                self.pbar.write(progress)
                            logger.warning(progress)
                            break
                    elif self.move:
                        try:
                            self.files_moved += 1
                            if not self.dry_run:
                                shutil.move(filename, target_file)
                        except FileNotFoundError:
                            progress = f'{progress} => skipped, no such file or directory'
                            if self.progress:
                                self.pbar.write(progress)
                            logger.warning(progress)
                            break
                    elif self.link and not self.dry_run:
                        os.link(filename, target_file)
                    else:
                        try:
                            self.files_copied += 1
                            if not self.dry_run:
                                shutil.copy2(filename, target_file)
                        except FileNotFoundError:
                            progress = f'{progress} => skipped, no such file or directory'
                            if self.progress:
                                self.pbar.write(progress)
                            logger.warning(progress)
                            break

                    progress = f'{progress} => {target_file}'
                    if self.progress:
                        self.pbar.write(progress)
                    if not self.fast_mode:
                        logger.info(progress)

                    self.process_xmp(filename, target_file_name, suffix, output)
                    break

                suffix += 1
                target_split = os.path.splitext(target_file_path)
                target_file = f'{target_split[0]}-{suffix}{target_split[1]}'

        self.files_processed += 1
        if self.progress:
            self.pbar.update(1)

    def get_file_name_and_path(self, filename):
        """
        Returns target file name and path
        """
        if self.use_process_pool_for_exif:
            # Use a short-lived process pool for EXIF/date extraction when enabled.
            # This is most beneficial when EXIF parsing is CPU-bound.
            with concurrent.futures.ProcessPoolExecutor(max_workers=1) as pool:
                exif_data, target_file_type, date = pool.submit(
                    _extract_exif_and_date,
                    filename,
                    self.timestamp,
                    self.date_regex,
                    self.date_field,
                    self.ctime,
                ).result()
        else:
            exif_data = Exif(filename).data()
            target_file_type = None

            if exif_data and 'MIMEType' in exif_data:
                target_file_type = self.get_file_type(exif_data['MIMEType'])

            date = None

        camera_name = None
        if exif_data and self.camera_name_mode in ("prefix", "suffix"):
            make = exif_data.get('Make')
            model = exif_data.get('Model')
            if make or model:
                parts = [p for p in [make, model] if p]
                raw_name = " ".join(parts).strip()
                if raw_name:
                    # Normalize whitespace and remove problematic characters
                    name = re.sub(r'\s+', '-', raw_name)
                    name = name.replace('/', '-')
                    name = re.sub(r'[^A-Za-z0-9_-]+', '', name)
                    camera_name = name or None

        if target_file_type in ['image', 'video']:
            if date is None:
                date = Date(filename).from_exif(
                    exif_data, self.timestamp, self.date_regex,
                    self.date_field, ctime=self.ctime
                )
            output = self.get_output_dir(date)
            target_file_name = self.get_file_name(filename, date, camera_name=camera_name)
            if not self.original_filenames:
                target_file_name = target_file_name.lower()
        else:
            # Non-image / non-video files go to a dedicated "other" directory
            # when specified; otherwise retain the previous behavior of using
            # the "unknown" directory under the output root.
            output = self.get_other_dir()
            target_file_name = os.path.basename(filename)

        target_file_path = os.path.sep.join([output, target_file_name])
        return output, target_file_name, target_file_path, target_file_type, date

    def process_xmp(self, original_filename, file_name, suffix, output):
        """
        Process xmp files. These are metadata for RAW images
        """
        xmp_original_with_ext = original_filename + '.xmp'
        xmp_original_without_ext = os.path.splitext(original_filename)[0] + '.xmp'

        suffix = f'-{suffix}' if suffix > 1 else ''

        xmp_files = {}

        if os.path.isfile(xmp_original_with_ext):
            xmp_target = f'{file_name}{suffix}.xmp'
            xmp_files[xmp_original_with_ext] = xmp_target
        if os.path.isfile(xmp_original_without_ext):
            xmp_target = f'{(os.path.splitext(file_name)[0])}{suffix}.xmp'
            xmp_files[xmp_original_without_ext] = xmp_target

        for original, target in xmp_files.items():
            xmp_path = os.path.sep.join([output, target])
            logger.info(f'{original} => {xmp_path}')

            if not self.dry_run:
                if self.move:
                    shutil.move(original, xmp_path)
                elif self.link:
                    os.link(original, xmp_path)
                else:
                    shutil.copy2(original, xmp_path)
