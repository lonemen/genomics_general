#!/usr/bin/env python

import argparse
import sys
import gzip
import numpy as np
import itertools

import genomics

from multiprocessing import Process, Queue
from multiprocessing.queues import SimpleQueue
from threading import Thread
from time import sleep



####################################################################################################################################


'''A function that reads from the window queue, calls some other function and writes to the results queue
This function needs to be tailored to the particular analysis funcion(s) you're using. This is the function that will run on each of the N cores.'''
def stats_wrapper(windowQueue, resultQueue, windType, genoFormat, sampleData, minSites, stats, doPops, skipPairs, indHet):
    while True:
        windowNumber,window = windowQueue.get() # retrieve window
        if windType == "coordinate" or windType == "predefined":
            scaf,start,end,mid,sites = (window.scaffold, window.limits[0], window.limits[1], window.midPos(),window.seqLen())
        else: scaf,start,end,mid,sites = (window.scaffold, window.firstPos(), window.lastPos(),window.midPos(),window.seqLen())
        if sites >= minSites:
            isGood = True
            #make alignment object
            Aln = genomics.genoToAlignment(window.seqDict(), sampleData, genoFormat = genoFormat)
            statsDict = {}
            if doPops: statsDict.update(genomics.popDiv(Aln, doPairs = not skipPairs))
            if indHet:
                hetDict = Aln.sampleHet()
                for key in hetDict.keys(): hetDict["het_" + key] = hetDict.pop(key)
                statsDict.update(hetDict)
            values = [round(statsDict[stat], 4) for stat in stats]
        else:
            isGood = False
            values = [np.NaN]*len(stats)
        resultString = ",".join([str(x) for x in [scaf,start,end,mid,sites] + values])
        resultQueue.put((windowNumber, resultString, isGood))


'''a function that watches the result queue and sorts results. This should be a generic funcion regardless of the result, as long as the first object is the result number, and this increases consecutively.'''
def sorter(resultQueue, writeQueue, verbose):
  global resultsReceived
  sortBuffer = {}
  expect = 0
  while True:
    resNumber,result,isGood = resultQueue.get()
    resultsReceived += 1
    if verbose:
      print >> sys.stderr, "Sorter received result", resNumber
    if resNumber == expect:
      writeQueue.put((resNumber,result,isGood))
      if verbose:
        print >> sys.stderr, "Result", resNumber, "sent to writer"
      expect +=1
      #now check buffer for further results
      while True:
        try:
          result,isGood = sortBuffer.pop(str(expect))
          writeQueue.put((expect,result,isGood))
          if verbose:
            print >> sys.stderr, "Result", expect, "sent to writer"
          expect +=1
        except:
          break
    else:
      #otherwise this line is ahead of us, so add to buffer dictionary
      sortBuffer[str(resNumber)] = (result,isGood)

'''a writer function that writes the sorted result. This is also generic'''
def writer(writeQueue, out, writeFailedWindows=False):
    global resultsWritten
    global resultsHandled
    while True:
        resNumber,result,isGood = writeQueue.get()
        if verbose:
            print >> sys.stderr, "Writer received result", resNumber
        if isGood or writeFailedWindows:
            out.write(result + "\n")
            resultsWritten += 1
        resultsHandled += 1


'''loop that checks stats'''
def checkStats():
  while True:
    sleep(10)
    print >> sys.stderr, windowsQueued, "windows queued", resultsReceived, "results received", resultsWritten, "results written."


####################################################################################################################

parser = argparse.ArgumentParser()

parser.add_argument("--windType", help="Type of windows to make", action = "store", choices = ("sites","coordinate","predefined"), default = "coordinate")
parser.add_argument("-w", "--windSize", help="Window size in bases", type=int, action = "store", required = False, metavar="sites")
parser.add_argument("-s", "--stepSize", help="Step size for sliding window", type=int, action = "store", required = False, metavar="sites")
parser.add_argument("-m", "--minSites", help="Minumum good sites per window", type=int, action = "store", required = False, metavar="sites", default = 1)
parser.add_argument("-O", "--overlap", help="Overlap for sites sliding window", type=int, action = "store", required = False, metavar="sites")
parser.add_argument("-D", "--maxDist", help="Maximum span distance for sites window", type=int, action = "store", required = False)
parser.add_argument("--windCoords", help="Window coordinates file (scaffold start end)", required = False)

parser.add_argument("-p", "--population", help="Pop name and optionally sample names (separated by commas)",
                    required = False, action='append', nargs="+", metavar=("popName","[samples]"))
parser.add_argument("--popsFile", help="Optional file of sample names and populations", action = "store", required = False)
parser.add_argument("--samples", help="Samples to include for individual analysis", action = "store", metavar = "sample names")
parser.add_argument("--haploid", help="Samples that are haploid (comma separated)", action = "store", metavar = "sample names")
parser.add_argument("--skipPairs", help="Do not do pairwise statistics", action = "store_true")
parser.add_argument("--indHet", help="Calculate individual heterozygosity", action = "store_true")

parser.add_argument("-g", "--genoFile", help="Input genotypes file", required = False)
parser.add_argument("-o", "--outFile", help="Results file", required = False)
parser.add_argument("--exclude", help="File of scaffolds to exclude", required = False)
parser.add_argument("--include", help="File of scaffolds to analyse", required = False)
parser.add_argument("-f", "--genoFormat", help="Format of genotypes in genotypes file", action='store', choices = ("phased","pairs","haplo","diplo"), required = True)

parser.add_argument("-T", "--Threads", help="Number of worker threads for parallel processing", type=int, default=1, required = False, metavar="threads")
parser.add_argument("--verbose", help="Verbose output", action="store_true")
parser.add_argument("--writeFailedWindows", help="Write output even for windows with too few sites.", action="store_true")


args = parser.parse_args()

#window parameters
windType = args.windType

if args.windType == "coordinate":
    assert args.windSize, "Window size must be provided."
    windSize = args.windSize
    stepSize = args.stepSize
    if not stepSize: stepSize = windSize
    assert not args.overlap, "Overlap does not apply to coordinate windows. Use --stepSize instead."
    assert not args.maxDist, "Maximum distance only applies to sites windows."

elif args.windType == "sites":
    assert args.windSize, "Window size (number of sites) must be provided."
    windSize = args.windSize
    overlap = args.overlap
    if not overlap: overlap = 0
    maxDist = args.maxDist
    if not maxDist: maxDist = np.inf
    assert not args.stepSize, "Step size only applies to coordinate windows. Use --overlap instead."
else:
    assert args.windCoords, "Please provide a file of window coordinates."
    assert not args.overlap, "Overlap does not apply for predefined windows."
    assert not args.maxDist, "Maximum does not apply for predefined windows."
    assert not args.stepSize,"Step size does not apply for predefined windows."
    assert not args.include,"You cannot only include specific scaffolds if using predefined windows."
    assert not args.exclude,"You cannot exclude specific scaffolds if using predefined windows."
    with open(args.windCoords,"r") as wc: windCoords = tuple([(x,int(y),int(z),) for x,y,z in [line.split()[:3] for line in wc]])

minSites = args.minSites
if not minSites: minSites = windSize

#file info
genoFormat = args.genoFormat

outFileName = args.outFile

exclude = args.exclude
include = args.include

#other
threads = args.Threads
verbose = args.verbose


############## parse samples and populations
popNames = []
popInds = []
allInds = []
if args.population is not None:
    for p in args.population:
        popNames.append(p[0])
        if len(p) > 1: popInds.append(p[1].split(","))
        else: popInds.append([])

    if args.popsFile:
        with open(args.popsFile, "r") as pf: popDict = dict([ln.split() for ln in pf])
        for ind in popDict.keys():
            try: popInds[popNames.index(popDict[ind])].append(ind)
            except: pass

    for p in popInds: assert len(p) >= 1, "All populations must be represented by at least one sample."

    allInds += list(set([i for p in popInds for i in p]))
else: doPops = False

if args.samples is not None:
    allInds = list(set(allInds + args.samples.split(",")))
    args.indHet = True

assert doPops or args.indHet, "Populations not specified, and individual het not requested. Nothing to do."

#if populations and samples not specified, just get all sample names from file
if len(allInds) == 0:
    with gzip.open(args.genoFile, "r") if args.genoFile.endswith(".gz") else open(args.genoFile, "r") as gf:
        allInds = gf.readline().split()[2:]

ploidyDict = dict(zip(allInds,[2]*len(allInds)))

if args.haploid:
    for sample in args.haploid.split(","):
        ploidyDict[sample] = 1

sampleData = genomics.SampleData(indNames = allInds, popNames = popNames, popInds = popInds, ploidyDict = ploidyDict)

############################################################################################################################################

#open files

if args.genoFile: genoFile = gzip.open(args.genoFile, "r") if args.genoFile.endswith(".gz") else open(args.genoFile, "r")
else: genoFile = sys.stdin

if args.outFile: outFile = gzip.open(args.outFile, "w") if args.outFile.endswith(".gz") else open(args.outFile, "w")
else: outFile = sys.stdout

outFile.write("scaffold,start,end,mid,sites,")

############################################################################################################################################

#stats to output

stats = []

if args.indHet: stats += ["het_" + n for n in allInds]

if doPops:
    stats += ["pi_" + n for n in popNames]

    if not args.skipPairs:
        stats += ["dxy_" + x + "_" + y for x,y in itertools.combinations(popNames, 2)]
        stats += ["Fst_" + x + "_" + y for x,y in itertools.combinations(popNames, 2)]

outFile.write(",".join(stats) + "\n")

##############################################################

#scafs to exclude

if exclude:
    scafsFile = open(exclude, "rU")
    scafsToExclude = [line.rstrip() for line in scafsFile.readlines()]
    print >> sys.stderr, len(scafsToExclude), "scaffolds will be excluded."
    scafsFile.close()
else:
    scafsToExclude = None

if include:
    scafsFile = open(include, "rU")
    scafsToInclude = [line.rstrip() for line in scafsFile.readlines()]
    print >> sys.stderr, len(scafsToInclude), "scaffolds will be analysed."
    scafsFile.close()
else:
    scafsToInclude = None


##########################################################################################################

#counting stat that will let keep track of how far we are
windowsQueued = 0
resultsReceived = 0
resultsWritten = 0
resultsHandled = 0

'''Create queues to hold the data one will hold the line info to be passed to the analysis'''
windowQueue = SimpleQueue()
#one will hold the results (in the order they come)
resultQueue = SimpleQueue()
#one will hold the sorted results to be written
writeQueue = SimpleQueue()


'''start worker Processes for analysis. The comand should be tailored for the analysis wrapper function
of course these will only start doing anything after we put data into the line queue
the function we call is actually a wrapper for another function.(s) This one reads from the line queue, passes to some analysis function(s), gets the results and sends to the result queue'''
for x in range(threads):
  worker = Process(target=stats_wrapper, args = (windowQueue, resultQueue, windType, genoFormat, sampleData, minSites, stats, doPops, args.skipPairs, args.indHet,))
  worker.daemon = True
  worker.start()
  print >> sys.stderr, "started worker", x


'''thread for sorting results'''
worker = Thread(target=sorter, args=(resultQueue,writeQueue,verbose,))
worker.daemon = True
worker.start()

'''start thread for writing the results'''
worker = Thread(target=writer, args=(writeQueue, outFile, args.writeFailedWindows,))
worker.daemon = True
worker.start()


'''start background Thread that will run a loop to check run statistics and print
We use thread, because I think this is necessary for a process that watches global variables like linesTested'''
worker = Thread(target=checkStats)
worker.daemon = True
worker.start()




##########################################################

#get windows and analyse
if windType == "coordinate": windowGenerator = genomics.slidingCoordWindows(genoFile, windSize, stepSize,
                                                                            sampleData.indNames,
                                                                            include = scafsToInclude,
                                                                            exclude = scafsToExclude)
elif windType == "sites": windowGenerator = genomics.slidingSitesWindows(genoFile, windSize, overlap,
                                                                         maxDist, minSites, sampleData.indNames,
                                                                         include = scafsToInclude,
                                                                         exclude = scafsToExclude)
else: windowGenerator = genomics.predefinedCoordWindows(genoFile, windCoords, sampleData.indNames)


for window in windowGenerator:
    windowQueue.put((windowsQueued,window))
    windowsQueued += 1

############################################################################################################################################

print >> sys.stderr, "\nWriting final results...\n"
while resultsHandled < windowsQueued:
  sleep(1)

sleep(5)

genoFile.close()
outFile.close()

print >> sys.stderr, str(windowsQueued), "windows were tested.\n"
print >> sys.stderr, str(resultsWritten), "results were written.\n"

print "\nDone."

sys.exit()



