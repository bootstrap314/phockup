#!/usr/bin/env python3
import logging
import sys

from phockup import main, parse_args, setup_logging

logger = logging.getLogger('phockup')

if __name__ == '__main__':
    try:
        options = parse_args(sys.argv[1:])
        setup_logging(options)
        main(options)
    except Exception as e:
        logger.warning(e)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.error("Exiting phockup...")
        sys.exit(1)
    sys.exit(0)
