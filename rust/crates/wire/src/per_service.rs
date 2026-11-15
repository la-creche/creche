//! `PerService`, one value for each image of `ServiceImage` (browser
//! design 10.5.2). It is a Rust helper next to the enum, not a wire type:
//! it has no serde and no schema.
//!
//! A new service image fails to compile here until `PerService` holds it:
//! the match of `get` and `get_mut` names each variant, and `try_every`
//! and `iter` name each field. So no user of `PerService` holds a list of
//! the service images, and `get` cannot miss one.

use std::convert::Infallible;

use crate::ServiceImage;

/// One value for each image of `ServiceImage`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PerService<T> {
    browser: T,
}

impl<T> PerService<T> {
    /// The value `value` gives for each service image, in the order of the
    /// enum.
    pub fn every(mut value: impl FnMut(ServiceImage) -> T) -> Self {
        let Ok(every) = Self::try_every(|image| Ok::<_, Infallible>(value(image)));
        every
    }

    /// The value `value` gives for each service image, in the order of the
    /// enum.
    ///
    /// # Errors
    ///
    /// The first error of `value`.
    pub fn try_every<E>(mut value: impl FnMut(ServiceImage) -> Result<T, E>) -> Result<Self, E> {
        Ok(Self {
            browser: value(ServiceImage::Browser)?,
        })
    }

    /// The value of `image`.
    #[must_use]
    pub const fn get(&self, image: ServiceImage) -> &T {
        match image {
            ServiceImage::Browser => &self.browser,
        }
    }

    /// The value of `image`, to change.
    pub const fn get_mut(&mut self, image: ServiceImage) -> &mut T {
        match image {
            ServiceImage::Browser => &mut self.browser,
        }
    }

    /// Each service image with its value, in the order of the enum.
    pub fn iter(&self) -> impl Iterator<Item = (ServiceImage, &T)> {
        let Self { browser } = self;
        [(ServiceImage::Browser, browser)].into_iter()
    }

    /// The set with `map` applied to each value, in the order of the enum.
    ///
    /// # Errors
    ///
    /// The first error of `map`.
    pub fn try_map<U, E>(
        &self,
        mut map: impl FnMut(ServiceImage, &T) -> Result<U, E>,
    ) -> Result<PerService<U>, E> {
        PerService::try_every(|image| map(image, self.get(image)))
    }
}

#[cfg(test)]
mod tests {
    use super::PerService;
    use crate::{SandboxFlavor, ServiceImage};

    /// `every`, `get` and `iter` agree on the value of each service image,
    /// and `iter` gives each image once, in the order of the enum.
    #[test]
    fn every_get_and_iter_agree() {
        let every = PerService::every(|image| image);
        let listed: Vec<_> = every.iter().collect();
        assert_eq!(listed, [(ServiceImage::Browser, &ServiceImage::Browser)]);
        for (image, value) in every.iter() {
            assert_eq!(every.get(image), value);
        }
    }

    #[test]
    fn get_mut_changes_its_image() {
        let mut values = PerService::every(|_| 0_u8);
        *values.get_mut(ServiceImage::Browser) = 1;
        assert_eq!(*values.get(ServiceImage::Browser), 1);
    }

    #[test]
    fn try_map_keeps_each_image_or_gives_the_first_error() {
        let every = PerService::every(|_| 1_u8);
        let doubled = every.try_map(|_, value| Ok::<_, ()>(value * 2)).unwrap();
        assert!(doubled.iter().all(|(_, value)| *value == 2));
        let failed = every.try_map(|image, _| Err::<u8, _>(image));
        assert_eq!(failed, Err(ServiceImage::Browser));
    }

    /// `name` is the wire name of the serde derive, and it reads back as
    /// its own service image.
    #[test]
    fn name_is_the_wire_name() {
        for (image, _) in PerService::every(|_| ()).iter() {
            let wire = serde_json::to_value(image).unwrap();
            assert_eq!(wire, image.name());
            assert_eq!(serde_json::from_value::<ServiceImage>(wire).unwrap(), image);
        }
    }

    /// A service image is no flavor: no name of one set reads as a value of
    /// the other, so a family file cannot choose a service image and a key
    /// of `digests.json` names one set only.
    #[test]
    fn no_name_is_both_a_flavor_and_a_service_image() {
        for (image, _) in PerService::every(|_| ()).iter() {
            let name = serde_json::Value::from(image.name());
            assert!(serde_json::from_value::<SandboxFlavor>(name).is_err());
        }
    }
}
