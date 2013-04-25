==========
ibfupdater
==========

About
=====

The ‘Intelligent Background File Updater’ updates any files via cURL supported protocols and takes care of saving bandwidth. It can preserve previous versions of a file, so you can roll back manually when you need to. It is intelligent because it supports caching (ETag and Last-modified header) to save bandwith. It supports logging to syslog via ‘logger’ and is optimized for being run by cron jobs.


Usecase
=======

* You need to regulary update a file (e.g. a blocklist) and you want to do it intelligent (=not wasting yours and others bandwidth).


Features
========

* Supports multiple protocols (depends on your cURL installation)
* You can preserve previous versions
* Supports syslog
