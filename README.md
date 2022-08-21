# truenas_installer
Truenas Scale Installer scripts modified to support boot pool encryption by usin ZFSBootMenu instead of grub. With additional options such a limiting the boot pool partition size to enable the use of the extra space for a data partition, and to be able to unattended installations.
Based on:
* https://github.com/truenas/truenas-installer/blob/master/usr/sbin/truenas-install
* https://github.com/truenas/scale-build/blob/master/truenas_install/__main__.py
