// Our Z-index Orderer will lock the z-index of all browser windows we spawn. 
// This massively increases the amount of tabs we can spawn as the gui overlap problems
// for the os-level clicking are effectively negated. 

#![cfg(target_os = "windows")]

// Config

// Both rates are in ms. 
const ENFORCE_Z_ORDER_RATE: u64 = 1000;
const CHECK_NEW_PAGES_RATE: u64 = 5000;

use std::{
    cmp::Reverse,
    collections::HashSet,
    sync::{Arc, Mutex},
    thread,
    time::Duration,
};

use windows::Win32::{
    Foundation::{BOOL, HWND, LPARAM},
    UI::WindowsAndMessaging::{
        EnumWindows, GetWindowTextLengthW, IsIconic, IsWindowVisible,
        SetWindowPos, HWND_TOP, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, SWP_NOSENDCHANGING,
    },
};

// Note we need to implement unsafe Send and Sync for our hwnds, which have a *mut c_void.
// This is why we wrap them in a struct--because we can then give the struct these unsafe traits. 
// This will allow us to send the data to our loop/working threads: the z-order enforcement loop and the new page checker loop.
#[derive(Clone, Copy)]
struct SendHwnd(HWND);
unsafe impl Send for SendHwnd {}
unsafe impl Sync for SendHwnd {}

#[derive(Clone)]
struct TrackedWindow {
    hwnd: SendHwnd,
    z_index: usize,
}

type WindowList = Arc<Mutex<Vec<TrackedWindow>>>;

// Find all visible windows in a list of hwnds,
// and return a list of hwnds for these windows.
fn snapshot_visible() -> Vec<SendHwnd> {
    let mut hwnds: Vec<SendHwnd> = Vec::new();

    unsafe extern "system" fn callback(hwnd: HWND, lparam: LPARAM) -> BOOL {
        let list = &mut *(lparam.0 as *mut Vec<SendHwnd>);
        if IsWindowVisible(hwnd).as_bool() && !IsIconic(hwnd).as_bool() {
            if GetWindowTextLengthW(hwnd) > 0 {
                list.push(SendHwnd(hwnd));
            }
        }
        BOOL(1)
    }

    unsafe {
        let ptr = &mut hwnds as *mut Vec<SendHwnd> as isize;
        let _ = EnumWindows(Some(callback), LPARAM(ptr));
    }

    hwnds
}

fn z_index_of(hwnd: SendHwnd, visible: &[SendHwnd]) -> Option<usize> {
    let key = hwnd.0 .0 as isize;
    visible.iter().position(|h| h.0 .0 as isize == key)
}

// Sort z-index for each window so that it is below the z-index of the current (or z_prev + 1).
// We specifically insert it after it's predecessor.
// The setWindowPos method allows us to set the window position for a certain hwnd based on where we can to place it *after*, 
// which is why we take the previous entry. 
unsafe fn enforce_order(sorted: &[TrackedWindow]) {
    for i in (0..sorted.len()).rev() {
        let win = &sorted[i];
        if !IsWindowVisible(win.hwnd.0).as_bool() {
            continue;
        }
        let insert_after: HWND = if i == 0 { HWND_TOP } else { sorted[i - 1].hwnd.0 };
        SetWindowPos(
            win.hwnd.0,
            insert_after,
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOSENDCHANGING,
        )
        .ok();
    }
}

fn main() {
    // We store pre-existing windows and use this as a reference list of handles
    let pre_existing: HashSet<isize> = snapshot_visible()
        .into_iter()
        .map(|h| h.0 .0 as isize)
        .collect();

    let tracked: WindowList = Arc::new(Mutex::new(Vec::new()));

    // Enforce z-ordering for all windows.
    {
        let tracked = Arc::clone(&tracked);
        thread::spawn(move || loop {
            // Should be short while minimizing performance impact.
            thread::sleep(Duration::from_millis(ENFORCE_Z_ORDER_RATE)); 

            let list = tracked.lock().unwrap();
            if list.is_empty() {
                continue;
            }

            let live: Vec<TrackedWindow> = list
                .iter()
                .filter(|w| unsafe { IsWindowVisible(w.hwnd.0).as_bool() })
                .cloned()
                .collect();

            if live.len() >= 2 {
                unsafe { enforce_order(&live) };
            }
        });
    }

    // Check for new windows. 
    loop {
        // Should be long enough to not have performance impact while still polling.
        thread::sleep(Duration::from_millis(CHECK_NEW_PAGES_RATE)); 

        let current = snapshot_visible();
        let mut list = tracked.lock().unwrap();
        let known: HashSet<isize> = list.iter().map(|w| w.hwnd.0 .0 as isize).collect();

        // We must ensure pre existing windows are not including by this,
        // which we do by simply ensuring the hwnd isn't already found in the pre-existing windows list.
        let new_hwnds: Vec<SendHwnd> = current
            .iter()
            .copied()
            .filter(|h| {
                let hwnd = h.0 .0 as isize;
                !pre_existing.contains(&hwnd) && !known.contains(&hwnd)
            })
            .collect();

        for hwnd in new_hwnds {
            let z_index = z_index_of(hwnd, &current).unwrap_or(0);
            list.push(TrackedWindow { hwnd, z_index });
        }

        list.sort_by_key(|w| Reverse(w.z_index));
        list.retain(|w| unsafe { IsWindowVisible(w.hwnd.0).as_bool() });
    }
}
