// Native toString spoof helper.
const native_to_string = function toString() {
    return "function toString() { [native code] }";
};

// Inject a script to ensure window.matchMedia always returns true on "matches".
(function () {
    
    // Spoof native methods and their string representations.
    function create_mock_method(name) {
        return function (listener) {
            if (listener != null && typeof listener !== 'function' && typeof listener !== 'object') {
                throw new TypeError(`Failed to execute '${name}' on 'MediaQueryList': parameter 1 is not of type 'Object'.`);
            }
        };
    }

    const mock_match_media = function matchMedia(query) {
        
        // Take the native MediaQueryList prototype, we will patch these properties, as
        // MediaQueryList is returned by matchMedia.
        const query_string = typeof query === 'string' ? query : '';
        
        // Map the top-level properties and event listeners to match the native object.
        return {
            // true
            matches: true,
            // query data
            media: query_string,
            // null
            onchange: null,
            addListener: create_mock_method('addListener'),
            removeListener: create_mock_method('removeListener'),
            addEventListener: create_mock_method('addEventListener'),
            removeEventListener: create_mock_method('removeEventListener'),
            dispatchEvent: create_mock_method('dispatchEvent')
        };
    };

    // Spoof the string representations for the top-level matchMedia function.
    const match_media_to_string = function toString() {
        return "function matchMedia() { [native code] }";
    };

    Object.defineProperty(mock_match_media, 'toString', {
        value: match_media_to_string,
        configurable: true,
        writable: true,
        enumerable: false
    });

    Object.defineProperty(mock_match_media.toString, 'toString', {
        value: native_to_string,
        configurable: true,
        writable: true,
        enumerable: false
    });

    // Bind the completed mock to the prototype.
    Object.defineProperty(window, 'matchMedia', {
        value: mock_match_media,
        configurable: true,
        writable: true
    });
})();

window.addEventListener("message", (event) => {
    
    // Data must come from our own webpage.
    if (event.source != window || !event.data) return;

    if (event.data.type == "SET_TAB_PROXY") {
        
        // Spoof JS fields by injecting a script overriding the fields into the page.
        if (event.data.js_API_spoof_fields) {
            const fields = event.data.js_API_spoof_fields;
            
            // Create an isolated data store hidden inside this specific closure to prevent global Symbol leaking.
            const data_store = {};

            // Parse strings into primitive types if applicable.
            for (let key in fields) {
                let val = fields[key];
                if (val == "true") fields[key] = true;
                else if (val == "false") fields[key] = false;
                else if (typeof val === "string" && val.trim() != "" && !isNaN(Number(val))) fields[key] = Number(val);
            }

            for (let key in fields) {
                data_store[key] = fields[key];

                const parts = key.split(".");
                const prop = parts.pop();
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
                const target_obj = proto || obj;

                // Check if we have already locked this property on a previous proxy setup.
                if (!descriptor || !descriptor.get || !descriptor.get.___mocked) {
                    const mock_get = function () { return data_store[key]; };
                    const mock_set = function () {};

                    // Spoof the toString methods to perfectly mimic the native function strings that are returned
                    // by the JS when converting a native C++ function to its string representation.
                    const native_get_to_string = function () { return "function get " + prop + "() { [native code] }"; };
                    const native_set_to_string = function () { return "function set " + prop + "() { [native code] }"; };

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