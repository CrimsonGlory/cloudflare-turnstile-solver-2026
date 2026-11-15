//! Full-screen capture via X11 `GetImage`.
//!
//! Docker runs the clicker against Xvfb, so this talks to the root window on
//! `$DISPLAY` and never compiles a Wayland portal path. The previous
//! `screenshots` crate always built that path on Linux; rustc 1.100's never-type
//! fallback (`!` instead of `()`) made `dbus::blocking::Proxy::method_call`
//! fail with `!: ReadAll`.

use image::RgbaImage;
use xcb::x::{Drawable, GetImage, ImageFormat, ImageOrder};

#[derive(Debug, Clone, Copy)]
pub struct Screen {
    index: usize,
}

impl Screen {
    pub fn all() -> Result<Vec<Screen>, String> {
        let (conn, _) = xcb::Connection::connect(None).map_err(|e| e.to_string())?;
        let n = conn.get_setup().roots().count();
        if n == 0 {
            return Err("no X11 screens".into());
        }
        Ok((0..n).map(|index| Screen { index }).collect())
    }

    pub fn capture(&self) -> Result<RgbaImage, String> {
        let (conn, _) = xcb::Connection::connect(None).map_err(|e| e.to_string())?;
        let setup = conn.get_setup();
        let screen = setup
            .roots()
            .nth(self.index)
            .ok_or_else(|| format!("X11 screen {} missing", self.index))?;

        let width = u32::from(screen.width_in_pixels());
        let height = u32::from(screen.height_in_pixels());
        if width == 0 || height == 0 {
            return Err("X11 screen has zero size".into());
        }

        let cookie = conn.send_request(&GetImage {
            format: ImageFormat::ZPixmap,
            drawable: Drawable::Window(screen.root()),
            x: 0,
            y: 0,
            width: screen.width_in_pixels(),
            height: screen.height_in_pixels(),
            plane_mask: u32::MAX,
        });
        let reply = conn.wait_for_reply(cookie).map_err(|e| e.to_string())?;
        let bit_order = setup.bitmap_format_bit_order();
        let depth = reply.depth();
        let pixmap_format = setup
            .pixmap_formats()
            .iter()
            .find(|item| item.depth() == depth)
            .ok_or_else(|| format!("no pixmap format for depth {depth}"))?;
        let bits_per_pixel = u32::from(pixmap_format.bits_per_pixel());

        rgba_from_zpixmap(reply.data(), width, height, depth, bits_per_pixel, bit_order)
    }
}

fn rgba_from_zpixmap(
    bytes: &[u8],
    width: u32,
    height: u32,
    depth: u8,
    bits_per_pixel: u32,
    bit_order: ImageOrder,
) -> Result<RgbaImage, String> {
    if bits_per_pixel == 0 || bits_per_pixel % 8 != 0 {
        return Err(format!("unsupported bits_per_pixel {bits_per_pixel}"));
    }
    let src_stride = (bits_per_pixel / 8) as usize;
    let n_pixels = (width as usize).saturating_mul(height as usize);
    let needed = n_pixels.saturating_mul(src_stride);
    if bytes.len() < needed {
        return Err(format!(
            "GetImage short buffer: {} < {} ({}x{}, bpp {bits_per_pixel})",
            bytes.len(),
            needed,
            width,
            height
        ));
    }

    let get_pixel = match depth {
        8 => pixel8_rgba,
        16 => pixel16_rgba,
        24 | 32 => pixel24_32_rgba,
        _ => return Err(format!("unsupported depth {depth}")),
    };

    let mut rgba = vec![0u8; n_pixels * 4];
    for i in 0..n_pixels {
        let (r, g, b, a) = get_pixel(bytes, i * src_stride, bit_order);
        let o = i * 4;
        rgba[o] = r;
        rgba[o + 1] = g;
        rgba[o + 2] = b;
        rgba[o + 3] = a;
    }

    RgbaImage::from_raw(width, height, rgba).ok_or_else(|| "invalid RGBA buffer".into())
}

fn pixel8_rgba(bytes: &[u8], index: usize, bit_order: ImageOrder) -> (u8, u8, u8, u8) {
    let pixel = if bit_order == ImageOrder::LsbFirst {
        bytes[index]
    } else {
        bytes[index] & (7 << 4) | bytes[index] >> 4
    };
    let r = (pixel >> 6) as f32 / 3.0 * 255.0;
    let g = ((pixel >> 2) & 7) as f32 / 7.0 * 255.0;
    let b = (pixel & 3) as f32 / 3.0 * 255.0;
    (r as u8, g as u8, b as u8, 255)
}

fn pixel16_rgba(bytes: &[u8], index: usize, bit_order: ImageOrder) -> (u8, u8, u8, u8) {
    let pixel = if bit_order == ImageOrder::LsbFirst {
        u16::from(bytes[index]) | u16::from(bytes[index + 1]) << 8
    } else {
        u16::from(bytes[index]) << 8 | u16::from(bytes[index + 1])
    };
    let r = (pixel >> 11) as f32 / 31.0 * 255.0;
    let g = ((pixel >> 5) & 63) as f32 / 63.0 * 255.0;
    let b = (pixel & 31) as f32 / 31.0 * 255.0;
    (r as u8, g as u8, b as u8, 255)
}

fn pixel24_32_rgba(bytes: &[u8], index: usize, bit_order: ImageOrder) -> (u8, u8, u8, u8) {
    if bit_order == ImageOrder::LsbFirst {
        (bytes[index + 2], bytes[index + 1], bytes[index], 255)
    } else {
        (bytes[index], bytes[index + 1], bytes[index + 2], 255)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bgra_lsb_converts_to_rgba() {
        // One 32-bpp pixel stored B,G,R,pad.
        let bytes = [10, 20, 30, 0];
        let img = rgba_from_zpixmap(&bytes, 1, 1, 24, 32, ImageOrder::LsbFirst).unwrap();
        assert_eq!(img.get_pixel(0, 0).0, [30, 20, 10, 255]);
    }

    #[test]
    fn rgb_msb_converts_to_rgba() {
        let bytes = [30, 20, 10, 0];
        let img = rgba_from_zpixmap(&bytes, 1, 1, 32, 32, ImageOrder::MsbFirst).unwrap();
        assert_eq!(img.get_pixel(0, 0).0, [30, 20, 10, 255]);
    }

    #[test]
    fn short_buffer_is_an_error() {
        let err = rgba_from_zpixmap(&[1, 2, 3], 1, 1, 24, 32, ImageOrder::LsbFirst).unwrap_err();
        assert!(err.contains("short buffer"), "{err}");
    }
}
