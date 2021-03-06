#!/usr/bin/env python

"""

multi_cloud_mirror.py
(c) Copyright 2011 Joe Masters Emison
Licensed under the Mozilla Public License (MPL) v1.1
Full license terms available at http://www.mozilla.org/MPL/MPL-1.1.html

Updated to use Pyrax by Juey C. Ong.

multi_cloud_mirror provides an easy, multi-processing way of synchronizing
a bucket at Amazon S3 to a container at Rackspace Cloud Files, or vice
versa.

"""

#######################################################################
### Imports
#######################################################################
import boto
import pyrax
import smtplib
import os
import sys
import time
import datetime
import argparse
import configparser
import multiprocessing
from boto.exception import S3ResponseError, S3PermissionsError, S3CopyError
from pyrax.exceptions import (ClientException, NoSuchContainer, AuthenticationFailed, NoSuchObject, InvalidSize)
from configparser import NoSectionError, NoOptionError, MissingSectionHeaderError, ParsingError
from email.mime.text import MIMEText
from subprocess import Popen, PIPE

#######################################################################
### Global Functions
#######################################################################
def connectToClouds():
   """
   Open connections to S3 and Cloud Files
   """
   s3Conn = None
   cfConn = None
   try:
      ## boto reads from /etc/boto.cfg (or ~/boto.cfg)
      s3Conn = boto.connect_s3()
      ## the cloud files library doesn't automatically read from a file, so we handle that here:
      cfConfig = configparser.ConfigParser()
      cfConfig.read('/etc/cloudfiles.cfg')
      pyrax.set_setting("identity_type", "rackspace")
      pyrax.set_default_region(cfConfig.get('Credentials','region'))
      pyrax.set_credentials(cfConfig.get('Credentials','username'), cfConfig.get('Credentials','api_key'))
      cfConn = pyrax.connect_to_cloudfiles(cfConfig.get('Credentials','region'))
   except (NoSectionError, NoOptionError, MissingSectionHeaderError, ParsingError) as err:
      raise MultiCloudMirrorException("Error in reading Cloud Files configuration file (/etc/cloudfiles.cfg): %s" % (err))
   except (S3ResponseError, S3PermissionsError) as err:
      raise MultiCloudMirrorException("Error in connecting to S3: [%d] %s" % (err.status, err.reason))
   except (ClientException, AuthenticationFailed) as err:
      raise MultiCloudMirrorException("Error in connecting to CF: %s" % str(err))
   return (s3Conn, cfConn)


#######################################################################
### Copy functions
#######################################################################
def copyToS3(srcBucketName, myKeyName, destBucketName,tmpDir):
   """
   Copy files to S3 from CF, given a source container and key, 
   and a destination bucket and temporary file for local storage
   """
   # because of the way S3 and boto work, we have to save to a local file first, then upload to Cloud Files
   # note that maximum file size (as of this writing) for Cloud Files is 5GB, and we expect 6+GB free on the drive
   (s3Conn, cfConn) = connectToClouds()
   tmpFile = str(tmpDir + '/' + myKeyName)
   pyrax.cloudfiles.download_object(cfConn.get_container(srcBucketName), myKeyName, tmpDir)
   destBucket = s3Conn.get_bucket(destBucketName)
   newObj = None
   try:
      newObj = destBucket.new_key(myKeyName)
   except S3ResponseError:
      # key may exist; just get it instead:
      newObj = destBucket.get_key(myKeyName)
   newObj.set_contents_from_filename(tmpFile,replace=True)
   os.remove(tmpFile)


def copyToCF(srcBucketName, myKeyName, destBucketName):
   """
   Copy files to CF from S3, given a source bucket and key,
   and a destination container
   """
   # we can stream from S3 to Cloud Files, saving us from having to write to disk
   (s3Conn, cfConn) = connectToClouds()
   srcBucket  = s3Conn.get_bucket(srcBucketName)
   destBucket = cfConn.get_container(destBucketName)
   #with S3, we must request the key singly to get its metadata:
   fullKey = srcBucket.get_key(myKeyName)
   # create new Cloud Files object and stream the file from S3 to Cloud Files
   content_type = fullKey.content_type or fullKey.DefaultContentType
   newObj = destBucket.create(obj_name=myKeyName, data=fullKey.get_contents_as_string(), content_type=content_type)

#######################################################################
### Delete functions
#######################################################################
def deleteFromS3(bucketName, myKeyName):
   """
   Delete a file from S3, given a container and key
   """
   (s3Conn, cfConn) = connectToClouds()
   bucket = s3Conn.get_bucket(bucketName)
   bucket.delete_key(myKeyName)

def deleteFromCF(bucketName, myKeyName):
   """
   Delete a file from CF, given a container and key
   """
   (s3Conn, cfConn) = connectToClouds()
   bucket = cfConn.get_container(bucketName)
   bucket.delete_object(myKeyName)


#######################################################################
### MultiCloudMirror Classes
#######################################################################
class MultiCloudMirrorException(Exception):
   pass

class MultiCloudMirror:
   LOG_CRIT=3
   LOG_WARN=2
   LOG_INFO=1
   LOG_DEBUG=0
   CF_MAX_OBJECTS_IN_LIST=10000

   def __init__(self, sync=None, numProcesses=4, maxFileSize=5368709120, emailDest='', emailSrc='',
                emailSubj="[Multi-Cloud Mirror] Script Run at %s" % (str(datetime.datetime.now())),
                tmpDir='/tmp', debug=0, sendmail=0, delete=0, maxFileDeletion=10, minFileSync=10):
      # initialize variables
      self.debug              = debug
      self.emailMsg           = ""
      self.sync               = sync
      self.maxFileSize        = maxFileSize
      self.emailDest          = emailDest
      self.emailSrc           = emailSrc
      self.emailSubj          = emailSubj
      self.sendmail           = sendmail
      self.tmpDir             = tmpDir
      self.s3Conn             = None
      self.cfConn             = None
      self.pool               = multiprocessing.Pool(numProcesses)
      self.jobs               = []
      self.jobCount           = 0
      self.srcList            = []
      self.destList           = []
      self.filesAtSource      = {}
      self.filesAtDestination = {}
      self.delete             = delete
      self.maxFileDeletion    = maxFileDeletion
      self.minFileSync        = minFileSync
      self.syncCount          = 0
      self.copyCount          = 0
      self.deleteCount        = 0

   def logItem(self, msg, level):
      """
      Log function for MultiCloudMirror class: email and printing to screen
      """
      if level >= 0:
         if self.debug: 
            print (msg)
            sys.stdout.flush()
         if level >= 1:
            self.emailMsg += msg + "\n"


   def getScenarioDetails(self,scenario):
      """
      Take a scenario input and break it into component pieces; log error
      """
      [fromBucket, toBucket] = scenario.split('->')
      srcService = fromBucket[:2].lower()
      destService = toBucket[:2].lower()
      srcBucketName  = fromBucket[5:]
      destBucketName = toBucket[5:]
      serviceError = None
      # Validate Inputs
      if srcService not in ['cf','s3']:
         serviceError ="Source service not recognized."
      elif destService not in ['cf','s3']:
         serviceError ="Destination service not recognized."
      elif srcService == destService:
         serviceError ="Same-cloud mirroring not supported."
      self.logItem("\nScenario: %s; (from: %s in %s , to: %s in %s)" % (scenario, srcBucketName, srcService, destBucketName, destService), self.LOG_INFO)
      return(srcService, srcBucketName, destService, destBucketName, serviceError)


   def connectToBuckets(self, srcService, srcBucketName, destBucketName):
      """
      Open connections and list files in buckets/containers
      """
      # There's a limit in Cloud Files per listing of objects, so we get the Cloud Files list here
      # (to maximize code reuse)
      cfBucketName = destBucketName if srcService == 's3' else srcBucketName
      # Because the cloudfiles.ObjectResults class can't easily be appended to, we make a new list
      cfList = []
      cfList.extend(self.cfConn.get_container(cfBucketName).get_objects())
      lastLen = len(cfList)
      while lastLen == self.CF_MAX_OBJECTS_IN_LIST:
         cfList.extend(self.cfConn.get_container(cfBucketName).get_objects(marker=cfList[-1].name))
         lastLen = len(cfList) - lastLen
      # Now assign bucket/container lists to class lists
      if srcService == 's3':
         self.srcList        = self.s3Conn.get_bucket(srcBucketName).list()
         self.destList       = cfList
      elif srcService == 'cf':
         self.srcList        = cfList
         self.destList       = self.s3Conn.get_bucket(destBucketName).list()
      # Loop through the files at the destination
      for dKey in self.destList:
         myKeyName = getattr(dKey, 'key', dKey.name)
         self.filesAtDestination[myKeyName] = dKey.etag.replace('"','')

   def checkAndDelete(self, dKey, destService, destBucketName):
      """
      Check to see if this file should be deleted, and, if so, queue it
      """
      myKeyName = getattr(dKey, 'key', dKey.name)

      # if delete is disabled, or maxFileDeletion has reached limit
      if (self.delete == 0):
         return
      if (self.maxFileDeletion == 0):
         return
      if (self.syncCount < self.minFileSync):
         return

      # skip S3 "folders", since Cloud Files doesn't support them, and skip files that are too large
      self.logItem("Found %s at source" % (myKeyName), self.LOG_DEBUG)
      if myKeyName[-1] == '/':
         self.logItem("Skipping %s because it is a 'folder'" % (myKeyName), self.LOG_DEBUG)
         return
      if (dKey.size > self.maxFileSize):
         self.logItem("Skipping %s because it is too large (%d bytes)" % (myKeyName, dKey.size), self.LOG_WARN)
         return
               
      # Delete if file does not exist at source
      doDelete = False
      if myKeyName not in self.filesAtSource:
         # the file not present in the source, so should be deleted
         doDelete = True
         self.logItem("...Missing at source, so it will be deleted", self.LOG_DEBUG)

      if doDelete:
         # add delete job to pool
         self.jobCount += 1
         job = None
         if destService == "s3":
            job = self.pool.apply_async(deleteFromS3, (destBucketName, myKeyName))
         elif destService == "cf":
            job = self.pool.apply_async(deleteFromCF, (destBucketName, myKeyName))
         job_dict = dict(job=job, task="delete", myKeyName=myKeyName, destService=destService, destBucketName=destBucketName)
         self.jobs.append(job_dict)
         self.deleteCount = self.deleteCount + 1
         if (self.maxFileDeletion > 0):
            self.maxFileDeletion = self.maxFileDeletion - 1
            if (self.maxFileDeletion == 0):
               self.logItem("...Reached max file deletion limit - any further files will be ignored", self.LOG_INFO)
               
      else:
         # if we did not need to copy the file, log it:
         self.logItem("...Found at source, so it will not be deleted", self.LOG_DEBUG)

   def checkAndCopy(self, sKey, srcService, srcBucketName, destService, destBucketName):
      """
      Check to see if this file should be copied, and, if so, queue it
      """
      try:
         myKeyName = getattr(sKey, 'key', sKey.name)
         self.filesAtSource[myKeyName] = sKey.etag.replace('"','')
      except ClientException as err:
         self.logItem("Skipping %s because Pyrax ClientException: %s" % (myKeyName, str(err)), self.LOG_DEBUG)
         return
      except Exception as err:
         raise Exception ("Error in getting key attribute: %s" % str(err))
         return

      # skip S3 "folders", since Cloud Files doesn't support them, and skip files that are too large
      self.logItem("Found %s at source" % (myKeyName), self.LOG_DEBUG)
      if myKeyName[-1] == '/':
         self.logItem("Skipping %s because it is a 'folder'" % (myKeyName), self.LOG_DEBUG)
         return
      if srcService == "cf":
         cfBucketName = srcBucketName
         s3BucketName = destBucketName
         sKey.size = sKey.total_bytes
      else:
         s3BucketName = srcBucketName
         cfBucketName = destBucketName
      if (sKey.size > self.maxFileSize):
         self.logItem("Skipping %s because it is too large (%d bytes)" % (myKeyName, sKey.size), self.LOG_WARN)
         return
      # Copy if MD5 (etag) values are different, or if file does not exist at destination
      doCopy = False;
      try:
         self.logItem("Source md5: %s" % self.filesAtSource[myKeyName], self.LOG_DEBUG)
         self.logItem("Dest   md5: %s" % self.filesAtDestination[myKeyName], self.LOG_DEBUG)
         if self.filesAtDestination[myKeyName] != self.filesAtSource[myKeyName]:
            # the file is at the destination, but the md5sums do not match, so overwrite
            doCopy = True
            self.logItem("...Found at destination, but md5sums did not match, so it will be copied", self.LOG_DEBUG)
      except KeyError:
         doCopy = True
         self.logItem("...Not found at destination, so it will be copied", self.LOG_DEBUG)
      if doCopy:
         # add copy job to pool
         self.jobCount += 1
         job = None
         if srcService == "s3":
            job = self.pool.apply_async(copyToCF, (srcBucketName, myKeyName, destBucketName))
         elif srcService == "cf":
            job = self.pool.apply_async(copyToS3, (srcBucketName, myKeyName, destBucketName, self.tmpDir))
         job_dict = dict(job=job, task="copy", myKeyName=myKeyName, srcService=srcService, srcBucketName=srcBucketName, destBucketName=destBucketName, destService=destService, cfBucketName=cfBucketName, s3BucketName=s3BucketName)
         self.jobs.append(job_dict)
         self.copyCount = self.copyCount + 1
      else:
         # if we did not need to copy the file, log it:
         self.logItem("...Found at destination and md5sums match, so it will not be copied", self.LOG_DEBUG)
         self.syncCount = self.syncCount + 1

   def waitForJobstoFinish(self):
      """
      Loop through jobs, waiting for them to end
      """
      allFinished = False
      while not allFinished:
         # Check the status of the jobs.
         if self.jobs:
            self.logItem("Checking status of %d remaining tasks at %s" % (len(self.jobs), str(datetime.datetime.now())), self.LOG_DEBUG)
            for job_dict in self.jobs:
               job = job_dict['job']
               if job.ready():
                  # If the job finished but failed, note the exception
                  if not job.successful():
                     try:
                        job.get() # This will re-raise the exception.
                     except (S3ResponseError, S3PermissionsError, S3CopyError) as err:
                        self.logItem("Error in %s %s to/from S3 bucket %s: [%d] %s" % (job_dict['task'], job_dict['myKeyName'], job_dict['s3BucketName'], err.status, err.reason), self.LOG_WARN)
                        self.jobs.remove(job_dict)
                     except (ClientException, NoSuchContainer, AuthenticationFailed, NoSuchObject) as err:
                        self.logItem("Error in %s %s to/from to CF container %s: %s" % (job_dict['task'], job_dict['myKeyName'], job_dict['cfBucketName'], str(err)), self.LOG_WARN)
                        self.jobs.remove(job_dict)
                     except MultiCloudMirrorException as err:
                        self.logItem("MultiCloudMirror error in %s %s: %s" % (job_dict['task'], job_dict['myKeyName'], str(err)), self.LOG_WARN)
                        self.jobs.remove(job_dict)
                     except Exception as err:
                        # even if we have an unknown error, we still want to forget about the job
                        self.logItem("Unknown error in %s %s: %s (%s)" % (job_dict['task'], job_dict['myKeyName'], str(err), str(err.args)), self.LOG_WARN)
                        self.jobs.remove(job_dict)
                  else:
                     self.logItem("%s %s [on/to %s]\n" % (job_dict['task'], job_dict['myKeyName'], job_dict['destService']), self.LOG_INFO)
                     self.jobs.remove(job_dict)
         # Exit when there are no jobs left.
         if not self.jobs:
            allFinished = True
         else:
            time.sleep(5)


   def sendStatusEmail(self):
      """
      Send status email if we have a from and to email address
      """
      if (self.emailDest is not None and self.emailSrc is not None):
          if (self.sendmail):

               msg = MIMEText(self.emailMsg)
               msg["From"] = self.emailSrc
               msg["To"] = self.emailDest
               msg["Subject"] = self.emailSubj
               p = Popen(["/usr/sbin/sendmail", "-t"], stdin=PIPE)
               p.communicate(msg.as_string())
          else:
               s = smtplib.SMTP('localhost')
               s.sendmail(self.emailSrc, self.emailDest.split(','), "From: %s\nTo: %s\nSubject: %s\n\n%s" %
                    (self.emailSrc, self.emailDest, self.emailSubj, self.emailMsg))
               s.quit()

          self.logItem("\nReport emailed to %s (from %s):\n----------------------\n%s\n----------------------\n"
                      % (self.emailDest, self.emailSrc, self.emailMsg), self.LOG_DEBUG)

   def run(self):
      """
      Run multi-cloud-mirroring
      """
      self.logItem("Multi-Cloud Mirror Script started at %s" % (str(datetime.datetime.now())), self.LOG_INFO)
      try:
         (self.s3Conn, self.cfConn) = connectToClouds()
      except MultiCloudMirrorException as err:
         self.logItem("MultiCloudMirror error on connect: %s" % (err), self.LOG_CRIT)
         raise
      # Cycle Through Requested Synchronizations
      for scenario in self.sync:
         [srcService, srcBucketName, destService, destBucketName, serviceError] = self.getScenarioDetails(scenario)
         if serviceError is not None:
            self.logItem(serviceError, self.LOG_WARN)
            continue
         # Connect to the proper buckets and retrieve file lists
         try:
            self.connectToBuckets(srcService, srcBucketName, destBucketName)
         except (S3ResponseError, S3PermissionsError) as err:
            self.logItem("Error in connecting to S3 bucket: [%d] %s" % (err.status, err.reason), self.LOG_WARN)
            continue
         except (ClientException, NoSuchContainer, AuthenticationFailed) as err:
            self.logItem("Error in connecting to CF container: %s" % str(err), self.LOG_WARN)
            continue
         # Iterate through files at the source to see which ones to copy, and put them on the multiprocessing queue:
         for sKey in self.srcList:
            self.checkAndCopy(sKey, srcService, srcBucketName, destService, destBucketName)
 
         runDelete = True           
         if (self.syncCount < self.minFileSync):
            runDelete = False
            self.logItem("Skipping file deletion as min number of files for sync was not reached", self.LOG_INFO)
      
         if runDelete:
            for dKey in self.destList:
               self.checkAndDelete(dKey, destService, destBucketName)
            
      self.waitForJobstoFinish()
      self.logItem("\n\n%s Files were previously mirrored %s Files Copied %s Files Deleted" % (self.syncCount, self.copyCount, self.deleteCount), self.LOG_INFO)
      self.logItem("\n\nMulti-Cloud Mirror Script ended at %s" % (str(datetime.datetime.now())), self.LOG_INFO)
      self.sendStatusEmail()



#######################################################################
### Run from the commandline
#######################################################################
if __name__ == '__main__':
   try:
      parser = argparse.ArgumentParser(description='Multi-Cloud Mirror Script')
      parser.add_argument('--process', dest='numProcesses',type=int, default=4,
                          help='number of simultaneous file upload threads to run')
      parser.add_argument('--maxsize', dest='maxFileSize',type=int, default=pyrax.object_storage.MAX_FILE_SIZE,
                          help='maximium file size to sync, in bytes (files larger than this size will be skipped)')
      parser.add_argument('--from', help='email address from which to send the status email; must be specified to receive message', dest='emailSrc')
      parser.add_argument('--to', dest='emailDest',
                          help='email address(es) (comma-separated) to which to send the status email; must be specificed to recieve message')
      parser.add_argument('--subject', help='subject of the status email', dest='emailSubj',
                          default="[Multi-Cloud Mirror] Script Run at %s" % (str(datetime.datetime.now())))
      parser.add_argument('--tmpdir', dest='tmpDir',
                          help='temporary directory used for writing when sending from cf to s3', default='/tmp')
      parser.add_argument('--debug', dest='debug', default=False, help='turn on debug output')
      parser.add_argument('--delete', dest='delete', default=False, help='delete destination files that do not exist in source')
      parser.add_argument('--maxdelete', dest='maxFileDeletion',type=int, default=10, help='max number of files that can be deleted (-1 for unlimited)')
      parser.add_argument('--minsync', dest='minFileSync',type=int, default=10, help='min number of files that are synced before delete process is allowed to run')
      parser.add_argument('--sendmail', dest='sendmail', default=False, help='use sendmail instead of local smtp')
      parser.add_argument('sync', metavar='"s3://bucket->cf://container"', nargs='+',
                          help='a synchronization scenario, of the form "s3://bucket->cf://container" or "cf://container->s3://bucket"')
      args = parser.parse_args()
      mcm = MultiCloudMirror(args.sync, args.numProcesses, args.maxFileSize, args.emailDest, args.emailSrc, args.emailSubj, args.tmpDir, args.debug, args.sendmail, args.delete, args.maxFileDeletion, args.minFileSync)
      mcm.run()
   except MultiCloudMirrorException as err:
      print ("Error from MultiCloudMirror: %s" % (str(err)))
