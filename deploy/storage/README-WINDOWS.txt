DashCam Pi Zero 2 W - Windows card handling
================================================

VALIDATION STATUS
This workflow has not yet been validated on Windows 10/11 or real dashcam media.

1. Never remove the microSD card while the Raspberry Pi is powered or recording.
2. Use "Prepare SD card for removal" and wait for the documented physical
   shutdown/power cue before removing the card.
3. Windows may show the FAT32 boot volume and the exFAT volume named DASHCAM.
4. Windows cannot normally read the Linux ext4 rootfs partition. If Windows asks
   to initialize, repair, or format an unknown partition, CANCEL the prompt.
5. Copy MP4 and matching JSON files from DASHCAM\clips or DASHCAM\protected.
   Keep each matching filename stem together.
6. Do not edit, rename, or delete files on the card unless the supported
   maintenance workflow explicitly permits it.

exFAT improves Windows interoperability; it does not guarantee survival of an
unsafe power loss. Keep backups of important protected clips.
