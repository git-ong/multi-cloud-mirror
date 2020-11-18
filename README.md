# multi-cloud-mirror
A Github copy of Joe Masters Emison's multi-cloud-mirror script, updated to
use Pyrax, Rackspace's official SDK for Python.

Forked from https://github.com/boxuk/multi-cloud-mirror.
See https://www.boxuk.com/insight/synchronising-assets-between-rackspace-and-s3/
for an article on the original work.

Also see commit (https://github.com/boxuk/multi-cloud-mirror/pull/1/commits/139d6310758067e3dcc991e92c52ff751c7a4c15)
for the README from the original Google Code repo.

The code has been updated to use `pyrax` instead of `python-cloudfiles` to support
Cloud Files regions in Rackspace. While some commit remarks may
suggest support for Python 3.x, this is not the case, as Pyrax requires
Python 2.7.x (https://developer.rackspace.com/sdks/python/).

To copy files in a region other than your Rackspace account's default Cloud Files region,
set the `region` attribute in the `/etc/cloudfiles.cfg` config file. Example to copy files
to/from the `DFW` region:

```
[Credentials]
username=your_username
api_key=your_api_key
region=DFW
```

Other than the Pyrax migration, The original work has been preserved as much as possible.
There is room for future improvement, such as:

- Support for objects greater than 5 GB.
- Network bandwith (and cost) optimization by skipping retrieval of object attributes, if possible.
- Support for Python 3 (one possible approach: replacing Pyrax with our own library using REST API calls - example: https://gist.github.com/bahostetterlewis/c84a08cd1f8dad74853c)
