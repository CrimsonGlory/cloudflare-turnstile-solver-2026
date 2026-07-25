// Inject a script to ensure window.matchMedia always returns true on "matches".
let match_media_script = document.createElement("script");
match_media_script.textContent = `
    (function() {
        let native_to_string = function toString() { return "function toString() { [native code] }"; };
        
        // Spoof native methods and their string representations.
        function create_mock_method(name) {
            let mock_fn = function(listener) {
                if (listener !== null && typeof listener !== 'function' && typeof listener !== 'object') {
                    throw new TypeError("Failed to execute '" + name + "' on 'MediaQueryList': parameter 1 is not of type 'Object'.");
                }
            };
            mock_fn.toString = function toString() { return "function " + name + "() { [native code] }"; };
            mock_fn.toString.toString = native_to_string;
            return mock_fn;
        }

        let mock_match_media = function matchMedia(query) { 
            // Take the native MediaQueryList prototype,
            // we will patch these properties since 
            // MediaQueryList is returned by matchMedia.
            let mql = Object.create(MediaQueryList.prototype);
            
            let query_string = typeof query === 'string' ? query : '';
            
            // Map the top-level properties and event listeners to match the native object
            Object.defineProperties(mql, {
                // true
                matches: { get: function() { return true; }, enumerable: true, configurable: true },
                // query data
                media: { get: function() { return query_string; }, enumerable: true, configurable: true },
                // null
                onchange: { value: null, writable: true, enumerable: true, configurable: true },
                
                // Apply the string prototype fix to all native listener methods.
                addListener: { value: create_mock_method('addListener'), writable: true, enumerable: true, configurable: true },
                removeListener: { value: create_mock_method('removeListener'), writable: true, enumerable: true, configurable: true },
                addEventListener: { value: create_mock_method('addEventListener'), writable: true, enumerable: true, configurable: true },
                removeEventListener: { value: create_mock_method('removeEventListener'), writable: true, enumerable: true, configurable: true },
                dispatchEvent: { value: create_mock_method('dispatchEvent'), writable: true, enumerable: true, configurable: true }
            });
            
            return mql;
        };
        
        // Spoof the string representations for the top-level matchMedia function.
        let match_media_to_string = function toString() { return "function matchMedia() { [native code] }"; };
        mock_match_media.toString = match_media_to_string;
        mock_match_media.toString.toString = native_to_string;
        
        // Bind the completed mock to the prototype.
        Object.defineProperty(Window.prototype, 'matchMedia', {
            value: mock_match_media,
            configurable: true,
            writable: true,    
            enumerable: true
        });
    })();
`;
document.documentElement.appendChild(match_media_script);
// Destroy script on completion.
match_media_script.remove();

window.addEventListener("message", (event) => {
    // Data must come from our own webpage.
    if (event.source != window || !event.data) return;

    if (event.data.type == "SET_TAB_PROXY") {
        
        let setup_promise = Promise.resolve();

        // Spoof JS fields by injecting a script overriding the fields into the page.
        if (event.data.js_API_spoof_fields) {
            setup_promise = new Promise((resolve) => {
                let script = document.createElement("script");
                
                // Wait for the injected script to signal that it has finished its execution,
                // delete the script and resolve once such occurs.
                document.addEventListener("JS_SPOOF_COMPLETE", function on_complete() {
                    document.removeEventListener("JS_SPOOF_COMPLETE", on_complete);
                    script.remove(); 
                    resolve();
                });

                script.textContent = `
                    (function() {
                        let fields = ${JSON.stringify(event.data.js_API_spoof_fields)};
                        
                        // Create an isolated data store hidden inside this specific closure to prevent global Symbol leaking.
                        if (!document.__spoof_manager_initialized) {
                            Object.defineProperty(document, '__spoof_manager_initialized', { 
                                value: true, 
                                enumerable: false 
                            });
                            
                            let data_store = {};
                            
                            document.addEventListener("__UPDATE_SPOOFS", (e) => {
                                let new_fields = e.detail;
                                for (let key in new_fields) {
                                    data_store[key] = new_fields[key];
                                    
                                    let parts = key.split(".");
                                    let prop = parts.pop();
                                    let obj = window;
                                    
                                    for (let i = 0; i < parts.length; i++) {
                                        if (!obj[parts[i]]) obj[parts[i]] = {}; 
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
                                        let mock_get = function() { return data_store[key]; };
                                        let mock_set = function() {};
                                        
                                        // Spoof the toString methods to perfectly mimic the native function strings that are returned
                                        // by the JS when converting a native C++ function to its string representation.
                                        let native_get_to_string = function() { return "function get " + prop + "() { [native code] }"; };
                                        let native_set_to_string = function() { return "function set " + prop + "() { [native code] }"; };
                                        let native_to_string_to_string = function() { return "function toString() { [native code] }"; };
                                        
                                        // We set our string spoofs so it looks just like what untampered JS would to Cloudflare.
                                        mock_get.toString = native_get_to_string;
                                        mock_set.toString = native_set_to_string;
                                        mock_get.toString.toString = native_to_string_to_string;
                                        mock_set.toString.toString = native_to_string_to_string;
                                        
                                        mock_get.___mocked = true;
                                        
                                        Object.defineProperty(target_obj, prop, {
                                            get: mock_get,
                                            set: mock_set,
                                            configurable: true, 
                                            enumerable: descriptor ? descriptor.enumerable : true
                                        });
                                    }
                                }
                            });
                        }
                        
                        // Parse strings into primitive types if applicable.
                        for (let key in fields) {
                            let val = fields[key];
                            if (val === "true") fields[key] = true;
                            else if (val === "false") fields[key] = false;
                            else if (typeof val == "string" && val.trim() != "" && !isNaN(Number(val))) fields[key] = Number(val);
                        }
                        
                        // Send the payload into our scoped closure.
                        document.dispatchEvent(new CustomEvent("__UPDATE_SPOOFS", { detail: fields }));
                        
                        // Signal back to the extension content script that this script execution is successfully completed.
                        document.dispatchEvent(new CustomEvent("JS_SPOOF_COMPLETE"));
                    })();
                `;
                
                document.documentElement.appendChild(script);
            });
        }

        // Wait for the JS injection to finish first before we proceed.
        setup_promise.then(() => {
            // Forward the requested proxy to the background.
            browser.runtime.sendMessage({
                action: "setup_proxy",
                proxy_details: event.data.proxy_details
            }).then((response) => {
                if (response && response.success) {
                    // Return message to the page telling the client the proxy is ready--async unlocks once received,
                    // giving us a mock lock/await.
                    window.postMessage({ type: "PROXY_READY" }, "*");
                }
            }).catch((err) => {
                console.error("Extension bridge error:", err);
            });
        });
    }
});