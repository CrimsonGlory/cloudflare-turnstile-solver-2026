// Native toString spoof helper.
let native_to_string = function toString() {
    return "function toString() { [native code] }";
};

window.addEventListener("message", (event) => {
    // Data must come from our own webpage.
    if (event.source != window || !event.data) return;

    if (event.data.type == "SET_TAB_PROXY") {
        // Spoof JS fields by injecting a script overriding the fields into the page.
        if (event.data.js_API_spoof_fields) {
            let fields = event.data.js_API_spoof_fields;
            
            // Create an isolated data store hidden inside this specific closure to prevent global Symbol leaking.
            let data_store = {};

            // Parse strings into primitive types if applicable.
            for (let key in fields) {
                let val = fields[key];
                if (val == "true") fields[key] = true;
                else if (val == "false") fields[key] = false;
                else if (typeof val === "string" && val.trim() != "" && !isNaN(Number(val))) fields[key] = Number(val);
            }

            for (let key in fields) {
                data_store[key] = fields[key];

                let parts = key.split(".");
                let prop = parts.pop();
                let obj = window;

                for (let i = 0; i < parts.length; i++) {
                    if (!obj[parts[i]]) {
                        obj[parts[i]] = {};
                    }
                    obj = obj[parts[i]];
                }

                // Locate the true prototype object, this ensures we don't apply our modifications
                // to the wrong instance.
                let proto = obj;
                let descriptor = null;
                while (proto && !descriptor) {
                    descriptor = Object.getOwnPropertyDescriptor(proto, prop);
                    if (!descriptor) proto = Object.getPrototypeOf(proto);
                }
                let target_obj = proto || obj;

                // Check if we have already locked this property on a previous proxy setup.
                if (!descriptor || !descriptor.get || !descriptor.get.___mocked) {
                    let mock_get = function () { return data_store[key]; };
                    let mock_set = function () {};

                    // Spoof the toString methods to perfectly mimic the native function strings that are returned
                    // by the JS when converting a native C++ function to its string representation.
                    let native_get_to_string = function () { return "function get " + prop + "() { [native code] }"; };
                    let native_set_to_string = function () { return "function set " + prop + "() { [native code] }"; };

                    // We set our string spoofs so it looks just like what untampered JS would to Cloudflare.
                    Object.defineProperty(mock_get, 'toString', { value: native_get_to_string, configurable: true, writable: true });
                    Object.defineProperty(mock_set, 'toString', { value: native_set_to_string, configurable: true, writable: true });
                    Object.defineProperty(mock_get.toString, 'toString', { value: native_to_string, configurable: true, writable: true });
                    Object.defineProperty(mock_set.toString, 'toString', { value: native_to_string, configurable: true, writable: true });

                    mock_get.___mocked = true;

                    Object.defineProperty(target_obj, prop, {
                        get: mock_get,
                        set: mock_set,
                        configurable: true,
                        enumerable: descriptor ? descriptor.enumerable : true
                    });
                }
            }
        }

        // Forward the requested proxy to the background.
        chrome.runtime.sendMessage({
            action: "setup_proxy",
            proxy_details: event.data.proxy_details
        }, (response) => {
            if (response && response.success) {
                // Return message to the page telling the client the proxy is ready--async unlocks once received,
                // giving us a mock lock/await.
                window.postMessage({ type: "PROXY_READY" }, "*");
            }
        });
    }
});
