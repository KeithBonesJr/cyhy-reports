#!/usr/bin/env python
# comment parts that generate and mail a report

"""Create CyHy notifications and email them out to CyHy points of contact.

Usage:
  create_send_notifications [options] CYHY_DB_SECTION
  create_send_notifications (-h | --help)

Options:
  -h --help              Show this message.
  --log-level=LEVEL      If specified, then the log level will be set to
                         the specified value.  Valid values are "debug",
                         "info", "warning", "error", and "critical".
                         [default: warning]
"""

import distutils.dir_util
import logging
import os
import subprocess
import sys

import docopt

from cyhy.core import Config
from cyhy.db import database
from cyhy.util import util
from cyhy_report.cyhy_notification import NotificationGenerator

current_time = util.utcnow()

NOTIFICATIONS_BASE_DIR = "/var/cyhy/reports/output"
NOTIFICATION_ARCHIVE_DIR = os.path.join(
    "notification_archive", "notifications{}".format(current_time.strftime("%Y%m%d"))
)
CYHY_MAILER_DIR = "/var/cyhy/cyhy-mailer"


def create_output_directories():
    """Create all necessary output directories."""
    distutils.dir_util.mkpath(
        os.path.join(NOTIFICATIONS_BASE_DIR, NOTIFICATION_ARCHIVE_DIR)
    )

def build_notifications_org_list(db):
    """Build notifications organization list.

    This is the list of organization IDs that should
    get a notification PDF for CYHY report types.
    """
    notifications_to_generate = set()
    cyhy_parent_ids = set()
    ticket_owner_ids = db.notifications.distinct("ticket_owner")
    for request in db.RequestDoc.collection.find({"_id": {"$in": ticket_owner_ids}, "report_types": "CYHY"}, {"_id":1}):
        notifications_to_generate.add(request["_id"])
        cyhy_parent_ids = cyhy_parent_ids | find_cyhy_parents(db, request["_id"])
        print("cyhy_parent_ids: %s" % cyhy_parent_ids)
    notifications_to_generate.update(cyhy_parent_ids)
    return notifications_to_generate
          
def find_cyhy_parents(db, org_id):
    """Find CYHY parents.

    Find weekly report types for CYHY parents
    recursively using the parent IDs.
    """
    cyhy_parents = set()
    for request in db.RequestDoc.collection.find({"children": org_id, "report_types": "CYHY"}, {"_id": 1}):
        print("Found CYHY Parent of OrgID %s is %s" % (org_id, request["_id"]))
        cyhy_parents.add(request["_id"])
        cyhy_parents.update(find_cyhy_parents(db, request["_id"]))
        print("Output of cyhy_parents is: %s " % cyhy_parents)
    return cyhy_parents


def generate_notification_pdfs(db, org_ids, master_report_key): 
    """Generate all notification PDFs for a list of organizations."""
    num_pdfs_created = 0
    for org_id in org_ids:
        logging.info("{} - Starting to create notification PDF".format(org_id))
        generator = NotificationGenerator(
            db, org_id, final=True, encrypt_key=master_report_key
        )
        was_encrypted, results = generator.generate_notification()
        if was_encrypted:
            num_pdfs_created += 1
            logging.info("{} - Created encrypted notification PDF".format(org_id))
        elif results is not None and len(results["notifications"]) == 0: 
            logging.info("{} - No notifications found, no PDF created".format(org_id))
        else:
            logging.error("{} - Unknown error occurred".format(org_id))
            return -1
    return num_pdfs_created


def main():
    """Set up logging and call the notification-related functions."""
    args = docopt.docopt(__doc__, version="1.0.0")
    # Set up logging
    log_level = args["--log-level"]
    try:
        logging.basicConfig(
            format="%(asctime)-15s %(levelname)s %(message)s", level=log_level.upper()
        )
    except ValueError:
        logging.critical(
            '"{}" is not a valid logging level.  Possible values '
            "are debug, info, warning, and error.".format(log_level)
        )
        return 1

    # Set up database connection
    db = database.db_from_config(args["CYHY_DB_SECTION"])

    # Create all necessary output subdirectories
    create_output_directories()

    # Change to the correct output directory
    os.chdir(os.path.join(NOTIFICATIONS_BASE_DIR, NOTIFICATION_ARCHIVE_DIR))

    # Create notification PDFs for CyHy orgs
    master_report_key = Config(args["CYHY_DB_SECTION"]).report_key
    num_pdfs_created = generate_notification_pdfs(db, cyhy_org_ids, master_report_key)
    logging.info("{} notification PDFs created".format(num_pdfs_created))

    # Create a symlink to the latest notifications.  This is for the
    # automated sending of notification emails.
    latest_notifications = os.path.join(
        NOTIFICATIONS_BASE_DIR, "notification_archive/latest"
    )
    if os.path.exists(latest_notifications):
        os.remove(latest_notifications)
    os.symlink(
        os.path.join(NOTIFICATIONS_BASE_DIR, NOTIFICATION_ARCHIVE_DIR),
        latest_notifications,
    )

    if num_pdfs_created:
        # Email all notification PDFs in
        # NOTIFICATIONS_BASE_DIR/notification_archive/latest
        os.chdir(CYHY_MAILER_DIR)
        p = subprocess.Popen(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.yml",
                "-f",
                "docker-compose.cyhy-notification.yml",
                "up",
            ],
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        data, err = p.communicate()
        return_code = p.returncode

        if return_code == 0:
            logging.info("Notification emails successfully sent")
        else:
            logging.error("Failed to email notifications")
            logging.error("Stderr report detail: %s%s", data, err)

        # Delete all NotificationDocs where generated_for is not []
        result = db.NotificationDoc.collection.delete_many(
            {"generated_for": {"$ne": []}}
        )
        logging.info(
            "Deleted {} notifications from DB (corresponding to "
            "those just emailed out)".format(result.deleted_count)
        )
    else:
        logging.info("Nothing to email - skipping this step")

    # Delete all NotificationDocs where ticket_owner is not a CyHy org, since
    # we are not currently sending out notifications for non-CyHy orgs
    result = db.NotificationDoc.collection.delete_many(
        {"ticket_owner": {"$nin": cyhy_org_ids}}
    )
    logging.info(
        "Deleted {} notifications from DB (owned by "
        "non-CyHy organizations, which do not currently receive "
        "notification emails)".format(result.deleted_count)
    )

    # Stop logging and clean up
    logging.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
