#!/usr/bin/env python
"""
To run the script you need to have credentials for:
1. https://urs.earthdata.nasa.gov (register for free if you don't have an account).
Place credentials into the file:
echo 'machine urs.earthdata.nasa.gov login USERNAME password PASSWORD' >& ~/.netrc

"""

import argparse
import dask
from dask.diagnostics import ProgressBar
import json
import logging
import os
from pathlib import Path

import boto3
import fsspec
import xarray as xr
from botocore.exceptions import ClientError

import hyp3_sdk as sdk
import numpy as np


HYP3_AUTORIFT_API = 'https://hyp3-autorift.asf.alaska.edu'

#
# Author: Mark Fahnestock
#
def point_to_prefix(dir_path: str, lat: float, lon: float) -> str:
    """
    Returns a string (for example, N78W124) for directory name based on
    granule centerpoint lat,lon
    """
    NShemi_str = 'N' if lat >= 0.0 else 'S'
    EWhemi_str = 'E' if lon >= 0.0 else 'W'

    outlat = int(10*np.trunc(np.abs(lat/10.0)))
    if outlat == 90: # if you are exactly at a pole, put in lat = 80 bin
        outlat = 80

    outlon = int(10*np.trunc(np.abs(lon/10.0)))

    if outlon >= 180: # if you are at the dateline, back off to the 170 bin
        outlon = 170

    dirstring = os.path.join(dir_path, f'{NShemi_str}{outlat:02d}{EWhemi_str}{outlon:03d}')
    return dirstring


class ASFTransfer:
    """
    Class to handle ITS_LIVE granule transfer from ASF to ITS_LIVE bucket.
    """
    def __init__(self, user: str, password: str, target_bucket: str, target_dir: str):
        self.hyp3 = sdk.HyP3(HYP3_AUTORIFT_API, user, password)
        self.target_bucket = target_bucket
        self.target_bucket_dir = target_dir

    def run(self, job_ids_file: str, chunks_to_copy: int):
        """
        Run the transfer of granules from ASF to ITS_LIVE S3 bucket.
        """
        job_ids = json.loads(job_ids_file.read_text())

        num_to_copy = len(job_ids)
        start = 0
        logging.info(f"{num_to_copy} granules to copy...")

        while num_to_copy > 0:
            num_tasks = chunks_to_copy if num_to_copy > chunks_to_copy else num_to_copy

            logging.info(f"Starting tasks {start}:{start+num_tasks}")
            tasks = [dask.delayed(self.copy_granule)(id) for id in job_ids[start:start+num_tasks]]
            assert len(tasks) == num_tasks
            results = None

            with ProgressBar():
                # Display progress bar
                results = dask.compute(tasks,
                                       scheduler="processes",
                                       num_workers=8)

            # logging.info(f"Results: {results}")
            for each_result in results[0]:
                logging.info("-->".join(each_result))

            num_to_copy -= num_tasks
            start += num_tasks

    @staticmethod
    def object_exists(bucket, key: str) -> bool:
        try:
            bucket.Object(key).load()

        except ClientError:
            return False

        return True

    def copy_granule(self, job_id):
        """
        Copy granule from source to target bucket if it does not exist in target
        bucket already.
        """
        job = self.hyp3.get_job_by_id(job_id)
        msgs = [f'Processing {job}']

        if job.running():
            msgs.append(f'WARNING: Job is still running! Skipping {job}')
            return msgs

        if job.succeeded():
            # get center lat lon
            with fsspec.open(job.files[0]['url']) as f:
                with xr.open_dataset(f) as ds:
                    lat = ds.img_pair_info.latitude[0]
                    lon = ds.img_pair_info.longitude[0]
                    msgs.append(f'Image center (lat, lon): ({lat}, {lon})')

            source = {'Bucket': job.files[0]['s3']['bucket'],
                      'Key': job.files[0]['s3']['key']}

            target_prefix = point_to_prefix(self.target_bucket_dir, lat, lon)
            target_key = f'{target_prefix}/{job.files[0]["filename"]}'

            bucket = boto3.resource('s3').Bucket(self.target_bucket)

            if self.object_exists(bucket, target_key):
                msgs.append(f'WARNING: {bucket.name}/{target_key} already exists! skipping {job}')

            else:
                bucket.copy(source, target_key)
                msgs.append(f'Copying {source["Bucket"]}/{source["Key"]} to {bucket.name}/{target_key}')
                # TODO: Need to copy anything else?

        else:
            msgs.append(f'WARNING: {job} failed!')
            # TODO: handle failures

        return msgs

def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-j', '--job-ids', type=Path, help='JSON list of HyP3 Job IDs')
    parser.add_argument('-n', '--chunks-to-copy', type=int, default=10, help='Number of granules to copy in parallel [%(default)d]')
    parser.add_argument('-t', '--target-bucket', help='Upload the autoRIFT products to this AWS bucket')
    parser.add_argument('-d', '--dir', help='Upload the autoRIFT products to this sub-directory of AWS bucket')
    parser.add_argument('-u', '--user', help='Username for https://urs.earthdata.nasa.gov login')
    parser.add_argument('-p', '--password', help='Password for https://urs.earthdata.nasa.gov login')
    args = parser.parse_args()

    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s',
                        datefmt='%m/%d/%Y %I:%M:%S %p', level=logging.INFO)

    transfer = ASFTransfer(args.user, args.password, args.target_bucket, args.dir)
    transfer.run(args.job_ids, args.chunks_to_copy)

if __name__ == '__main__':
    main()