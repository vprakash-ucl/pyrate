import os
import csv
import logging
import queue
import threading
import time
from datetime import datetime
from xml.etree import ElementTree
from pyrate import utils

ALGO = True
EXPORT_COMMANDS = [('run', 'parse messages from csv into the database.')]
INPUTS = ["aiscsv"]
OUTPUTS = ["aisdb", "baddata"]

def parse_timestamp(s):
    return datetime.strptime(s, '%Y%m%d_%H%M%S')

def int_or_null(s):
    if len(s) == 0:
        return None
    else:
        return int(s)

def float_or_null(s):
    if len(s) == 0 or s == 'None':
        return None
    else:
        return float(s)

def imostr(s):
    if len(s) > 20:
        return None
    return s

def longstr(s):
    if len(s) > 255:
        return s.substring(0, 254)
    return s

def set_null_on_fail(row, col, test):
    if not row[col] == None and not test(row[col]):
        row[col] = None

def check_imo(imo):
    return imo is None or utils.valid_imo(imo)

# column name constants
MMSI = 'MMSI'
TIME = 'Time'
MESSAGE_ID = 'Message_ID'
NAV_STATUS = 'Navigational_status'
SOG = 'SOG'
LONGITUDE = 'Longitude'
LATITUDE = 'Latitude'
COG = 'COG'
HEADING = 'Heading'
IMO = 'IMO'
DRAUGHT = 'Draught'
DEST = 'Destination'
VESSEL_NAME = 'Vessel_Name'
ETA_MONTH = 'ETA_month'
ETA_DAY = 'ETA_day'
ETA_HOUR = 'ETA_hour'
ETA_MINUTE = 'ETA_minute'

# specifies columns to take from raw data, and functions to convert them into
# suitable type for the database.
AIS_CSV_COLUMNS = [MMSI,
                   TIME,
                   MESSAGE_ID,
                   NAV_STATUS,
                   SOG,
                   LONGITUDE,
                   LATITUDE,
                   COG,
                   HEADING,
                   IMO,
                   DRAUGHT,
                   DEST,
                   VESSEL_NAME,
                   ETA_MONTH,
                   ETA_DAY,
                   ETA_HOUR,
                   ETA_MINUTE]

# xml names differ from csv. This array describes the names in this file which
# correspond to the csv column names
AIS_XML_COLNAMES = [
    'mmsi',
    'date_time',
    'msg_type',
    'nav_status',
    'sog',
    'lon',
    'lat',
    'cog',
    'heading',
    'imo',
    'draught',
    'destination',
    'vessel_name',
    'eta_month',
    'eta_day',
    'eta_hour',
    'eta_minute']

def xml_name_to_csv(name):
    """Converts a tag name from an XML file to the corresponding name from CSV."""
    return AIS_CSV_COLUMNS[AIS_XML_COLNAMES.index(name)]

def parse_raw_row(row):
    """Parse values from row, returning a new dict with values
    converted into appropriate types. Throw an exception to reject row"""
    converted_row = {}
    converted_row[MMSI] = int_or_null(row[MMSI])
    converted_row[TIME] = parse_timestamp(row[TIME])
    converted_row[MESSAGE_ID] = int_or_null(row[MESSAGE_ID])
    converted_row[NAV_STATUS] = int_or_null(row[NAV_STATUS])
    converted_row[SOG] = float_or_null(row[SOG])
    converted_row[LONGITUDE] = float_or_null(row[LONGITUDE])
    converted_row[LATITUDE] = float_or_null(row[LATITUDE])
    converted_row[COG] = float_or_null(row[COG])
    converted_row[HEADING] = float_or_null(row[HEADING])
    converted_row[IMO] = int_or_null(row[IMO])
    converted_row[DRAUGHT] = float_or_null(row[DRAUGHT])
    converted_row[DEST] = longstr(row[DEST])
    converted_row[VESSEL_NAME] = longstr(row[VESSEL_NAME])
    converted_row[ETA_MONTH] = int_or_null(row[ETA_MONTH])
    converted_row[ETA_DAY] = int_or_null(row[ETA_DAY])
    converted_row[ETA_HOUR] = int_or_null(row[ETA_HOUR])
    converted_row[ETA_MINUTE] = int_or_null(row[ETA_MINUTE])
    return converted_row

CONTAINS_LAT_LON = set([1, 2, 3, 4, 9, 11, 17, 18, 19, 21, 27])

def validate_row(row):
    # validate MMSI, message_id and IMO
    if not utils.valid_mmsi(row[MMSI]) or not utils.valid_message_id(row[MESSAGE_ID]) or not check_imo(row[IMO]):
        raise ValueError("Row invalid")
    # check lat long for messages which should contain it
    if row[MESSAGE_ID] in CONTAINS_LAT_LON:
        if not (utils.valid_longitude(row[LONGITUDE]) and utils.valid_latitude(row[LATITUDE])):
            raise ValueError("Row invalid (lat,long)")
    # otherwise set them to None
    else:
        row[LONGITUDE] = None
        row[LATITUDE] = None

    # validate other columns
    set_null_on_fail(row, NAV_STATUS, utils.valid_navigational_status)
    set_null_on_fail(row, SOG, utils.is_valid_sog)
    set_null_on_fail(row, COG, utils.is_valid_cog)
    set_null_on_fail(row, HEADING, utils.is_valid_heading)
    return row

def get_data_source(name):
    return 0

def run(inp, out, options={}):
    """Populate the AIS_Raw database with messages from the AIS csv files."""

    files = inp['aiscsv']
    db = out['aisdb']
    log = out['baddata']

    # drop indexes for faster insert
    db.clean.drop_indices()
    db.dirty.drop_indices()

    # queue for messages to be inserted into db
    dirtyq = queue.Queue(maxsize=5000)
    cleanq = queue.Queue(maxsize=5000)

    # worker thread which takes batches of tuples from the queue to be
    # inserted into db
    def sqlworker(q, table):
        while True:
            msgs = [q.get()]
            while not q.empty():
                msgs.append(q.get(timeout=0.5))

            n = len(msgs)
            if n > 0:
                #logging.debug("Inserting {} rows into {}".format(n, table.name))
                try:
                    table.insert_rows_batch(msgs)
                except Exception as e:
                    logging.warning("Error executing query: "+ repr(e))
            # mark this task as done
            for i in range(n):
                q.task_done()
            db.conn.commit()

    # set up processing pipeline threads
    clean_thread = threading.Thread(target=sqlworker, daemon=True,
                                   args=(cleanq, db.clean))
    dirty_thread = threading.Thread(target=sqlworker, daemon=True,
                                   args=(dirtyq, db.dirty))
    #validThread = threading.Thread(target=rowValidator, daemon=True, args=(validQ, cleanQ, dirtyQ))
    
    #validThread.start()
    dirty_thread.start()
    clean_thread.start()

    start = time.time()

    for fp, name, ext in files.iterfiles():
        # check if we've already parsed this file
        with db.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM " + db.sources.name + " WHERE filename = %s", [name])
            if cur.fetchone()[0] > 0:
                logging.info("Already parsed "+ name +", skipping...")
                continue

        logging.info("Parsing "+ name)
        
        # open error log csv file and write header
        errorlog = open(os.path.join(log.root, name), 'w')
        logwriter = csv.writer(errorlog, delimiter=',', quotechar='"')
        logwriter.writerow(AIS_CSV_COLUMNS + ["Error_Message"]) 

        # message counters
        clean_ctr = 0
        dirty_ctr = 0
        invalid_ctr = 0

        # Select the a file iterator based on file extension
        if ext == '.csv':
            iterator = readcsv
        elif ext == '.xml':
            iterator = readxml
        else:
            logging.warning("Cannot parse file with extension %s", ext)
            continue

        # infer the data source from the file name
        source = get_data_source(name)

        # parse and iterate lines from the current file
        for row in iterator(fp):
            converted_row = {}
            try:
                # parse raw data
                converted_row = parse_raw_row(row)
                converted_row['source'] = source
            except ValueError as e:
                # invalid data in row. Write it to error log
                logwriter.writerow([row[c] for c in AIS_CSV_COLUMNS] + ["{}".format(e)])
                invalid_ctr = invalid_ctr + 1
                continue

            # validate parsed row and add to appropriate queue
            try:
                validated_row = validate_row(converted_row)
                cleanq.put(validated_row)
                clean_ctr = clean_ctr + 1
            except ValueError:
                dirtyq.put(converted_row)
                dirty_ctr = dirty_ctr + 1

        db.sources.insert_row({'filename': name, 'ext': ext, 'invalid': invalid_ctr, 'clean': clean_ctr, 'dirty': dirty_ctr})

        errorlog.close()
        logging.info("Completed "+ name +": %d clean, %d dirty, %d invalid messages", clean_ctr, dirty_ctr, invalid_ctr)

    # wait for queued tasks to finish
    validq.join()
    dirtyq.join()
    cleanq.join()
    db.conn.commit()

    logging.info("Parsing complete, time elapsed = %fs", time.time() - start)

    start = time.time()

    logging.info("Rebuilding table indices...")
    db.clean.create_indices()
    db.dirty.create_indices()
    logging.info("Finished building indices, time elapsed = %fs", time.time() - start)

def readcsv(fp):
    # first line is column headers. Use to extract indices of columns we are extracting
    cols = fp.readline().split(',')
    indices = {}
    try:
        for col in AIS_CSV_COLUMNS:
            indices[col] = cols.index(col)
    except Exception as e:
        raise RuntimeError("Missing columns in file header: {}".format(e))

    for row in csv.reader(fp, delimiter=',', quotechar='"'):
        rowsubset = {}
        for col in AIS_CSV_COLUMNS:
            rowsubset[col] = row[indices[col]] # raw column data
        yield rowsubset

def readxml(fp):
    current = {}
    # iterate xml 'end' events
    for event, elem in ElementTree.iterparse(fp):
        # end of aismessage
        if elem.tag == 'aismessage':
            yield current
            current = {}
        else:
            if elem.tag in AIS_XML_COLNAMES and elem.text != None:
                current[xml_name_to_csv(elem.tag)] = elem.text
