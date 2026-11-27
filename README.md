# MyNice Home Assistant integration

This is an integration for adding Nice brand devices from the MyNice app to Home Assistant. It was made by reverse engineering the protocol of the mobile app.

Tested on **CU_WIFI** devices (i have the **SPIDER800W** garage opener and the **ROBUSR10** gate opener WiFi module, both tested and working), support for other types is unknown.

You need set up a MyNice and go through the process of pairing them to the app, which is quite a pain. It unfortunately does not work without a cloud account.

## Session recovery

The integration refreshes expired cloud tokens automatically and falls back to
the saved account credentials if the refresh token is rejected. Device socket
reconnects fetch fresh accessory credentials from the cloud. If a connected
accessory stops returning status updates for 75 seconds, the integration closes
the socket and reconnects. Opening the MyNice app should not be necessary for
these recovery paths.

After repeated failures, reconnect delays increase from 5 seconds to 10, 20, 40,
80, 160, then a maximum of 300 seconds between attempts. A valid device status
reply resets the delay to 5 seconds. Waiting retries are cancelled immediately
when the integration stops.

After updating the integration, restart Home Assistant to load the changes.
For troubleshooting, enable debug logging for `custom_components.mynice` and
look for connection-loss and accessory-connection messages.

The recovery regression tests run without Home Assistant installed:

```sh
python3 -m pip install aiohttp
python3 -m unittest discover -s tests -v
```
