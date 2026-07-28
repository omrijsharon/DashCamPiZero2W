# exFAT validation harnesses

`run.sh` is destructive only to three 64 MiB regular image copies made beneath
its private `/var/tmp/dashcam-exfat-fsck.*` directory and to loop devices whose
backing file and exact size it proves. It creates one formatted seed, then
exercises clean read-only checking, repair of a corrupted main boot-region
checksum from the intact backup region, and an invalid-root-cluster refusal. It
records pre/post whole-image hashes,
`wipefs`/`blkid` evidence, bounded fsck status/output hashes, and requires the
failed case to remain byte-identical. No fsck case invokes a formatter.

Run on the Pi only after reviewing the script and confirming no unrelated loop
work is active:

```sh
sudo /usr/bin/timeout -k 30 600 \
  /bin/bash /opt/dashcam-validation/exfat-fsck/run.sh |
  tee /tmp/dashcam-exfat-fsck-evidence.txt
```

`benchmark.sh` is separate: it intentionally tests the already-provisioned
recording volume, never a loop image. It verifies the exact card/mount identity,
requires the recorder to be inactive, reserves the greater of 2 GiB or 15%,
and caps total test data at 640 MiB. It writes only beneath a unique
`.validation-write-<UTC>-<pid>-<random>` directory, measures sustained and
eight burst write/finalization latencies, syncs every finalized file, and its
trap removes only its explicit filenames before removing that one empty
directory.

```sh
sudo /usr/bin/timeout -k 30 900 \
  /bin/bash /opt/dashcam-validation/exfat-fsck/benchmark.sh |
  tee /tmp/dashcam-exfat-write-evidence.txt
```

Review the evidence before treating either run as a milestone result. A local
script review or loop-image pass is not evidence for the real SD card's
performance or power-loss behavior.
