use std::ffi::{CString, c_char, c_int, c_void};
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::sync::OnceLock;

#[link(name = "vips")]
unsafe extern "C" {
    fn vips_init(argv0: *const c_char) -> c_int;
    fn vips_thumbnail(filename: *const c_char, out: *mut *mut c_void, width: c_int, ...) -> c_int;
    fn vips_image_new_from_file(filename: *const c_char, ...) -> *mut c_void;
    fn vips_conv(input: *mut c_void, out: *mut *mut c_void, mask: *mut c_void, ...) -> c_int;
    fn vips_image_write_to_file(image: *mut c_void, filename: *const c_char, ...) -> c_int;
}

#[link(name = "gobject-2.0")]
unsafe extern "C" {
    fn g_object_unref(object: *mut c_void);
}

struct Image(*mut c_void);

impl Drop for Image {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { g_object_unref(self.0) };
        }
    }
}

fn c_path(path: &Path) -> Option<CString> {
    CString::new(path.as_os_str().as_bytes()).ok()
}

pub fn thumbnail_and_sharpen(input: &Path, output: &Path, mask: &Path) -> bool {
    static INITIALIZED: OnceLock<bool> = OnceLock::new();
    if !*INITIALIZED.get_or_init(|| unsafe { vips_init(c"rustfire".as_ptr()) == 0 }) {
        return false;
    }
    let (Some(input), Some(output), Some(mask_path)) =
        (c_path(input), c_path(output), c_path(mask))
    else {
        return false;
    };
    let mut thumbnail = Image(std::ptr::null_mut());
    let resized = unsafe {
        vips_thumbnail(
            input.as_ptr(),
            &mut thumbnail.0,
            1200,
            c"height".as_ptr(),
            800 as c_int,
            c"size".as_ptr(),
            2 as c_int, // VIPS_SIZE_DOWN
            std::ptr::null::<c_void>(),
        )
    };
    if resized != 0 || thumbnail.0.is_null() {
        return false;
    }
    let mask =
        Image(unsafe { vips_image_new_from_file(mask_path.as_ptr(), std::ptr::null::<c_void>()) });
    if mask.0.is_null() {
        return false;
    }
    let mut sharpened = Image(std::ptr::null_mut());
    let convolved = unsafe {
        vips_conv(
            thumbnail.0,
            &mut sharpened.0,
            mask.0,
            c"precision".as_ptr(),
            0 as c_int, // VIPS_PRECISION_INTEGER
            std::ptr::null::<c_void>(),
        )
    };
    if convolved != 0 || sharpened.0.is_null() {
        return false;
    }
    unsafe {
        vips_image_write_to_file(sharpened.0, output.as_ptr(), std::ptr::null::<c_void>()) == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    #[test]
    fn in_process_output_matches_vips_cli() {
        let directory =
            std::env::temp_dir().join(format!("rustfire-vips-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&directory).unwrap();
        let mask = Path::new("static/vips-sharpen-mask.txt");
        let jpeg = directory.join("source.jpg");
        assert!(
            Command::new("vips")
                .args(["copy", "static/icons/app-icon-192.png"])
                .arg(&jpeg)
                .status()
                .unwrap()
                .success()
        );
        for (input, format) in [
            (Path::new("static/icons/app-icon-192.png"), "png"),
            (Path::new("static/sound-images/yeah.webp"), "webp"),
            (jpeg.as_path(), "jpg"),
        ] {
            let stage = directory.join("stage.v");
            let cli = directory.join(format!("cli.{format}"));
            let direct = directory.join(format!("direct.{format}"));
            assert!(
                Command::new("vips")
                    .arg("thumbnail")
                    .arg(input)
                    .arg(&stage)
                    .args(["1200", "--height", "800", "--size", "down"])
                    .status()
                    .unwrap()
                    .success()
            );
            assert!(
                Command::new("vips")
                    .arg("conv")
                    .arg(&stage)
                    .arg(&cli)
                    .arg(mask)
                    .args(["--precision", "integer"])
                    .status()
                    .unwrap()
                    .success()
            );
            assert!(thumbnail_and_sharpen(input, &direct, mask));
            assert_eq!(
                std::fs::read(cli).unwrap(),
                std::fs::read(direct).unwrap(),
                "{format}"
            );
        }
        std::fs::remove_dir_all(directory).unwrap();
    }
}
