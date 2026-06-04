(() => {
  const image = document.getElementById('zoom-image');
  const lens = document.getElementById('zoom-lens');
  const zoomFactor = 2;

  // Track whether zoom lens is visible
  let lensVisible = false;

  // For touch zoom pinch detection
  let lastDistance = null;

  // Show lens with fade-in
  function showLens() {
    if (!lensVisible) {
      lens.style.opacity = '1';
      lensVisible = true;
    }
  }

  // Hide lens with fade-out
  function hideLens() {
    if (lensVisible) {
      lens.style.opacity = '0';
      lensVisible = false;
    }
  }

  // Get cursor/touch position relative to the image
  function getPos(event) {
    const rect = image.getBoundingClientRect();
    
    // Adjust lens size for mobile
    if (window.innerWidth < 600) {
      lens.style.width = '100px';
      lens.style.height = '100px';
    } else {
      lens.style.width = '150px';
      lens.style.height = '150px';
    }

    let x, y;
    if (event.touches) {
      x = event.touches[0].clientX - rect.left;
      y = event.touches[0].clientY - rect.top;
    } else {
      x = event.clientX - rect.left;
      y = event.clientY - rect.top;
    }
    // Clamp x and y to image bounds
    x = Math.max(0, Math.min(x, rect.width));
    y = Math.max(0, Math.min(y, rect.height));
    return { x, y };
  }

  // Update lens position and background to create zoom effect
  function moveLens(x, y) {
    const rect = image.getBoundingClientRect();
    const lensWidth = lens.offsetWidth;
    const lensHeight = lens.offsetHeight;

    // Position lens centered on cursor/touch
    let lensX = x - lensWidth / 2;
    let lensY = y - lensHeight / 2;

    // Clamp lens position so it doesn't go outside image container
    lensX = Math.max(0, Math.min(lensX, rect.width - lensWidth));
    lensY = Math.max(0, Math.min(lensY, rect.height - lensHeight));

    lens.style.left = `${lensX}px`;
    lens.style.top = `${lensY}px`;

    // Background position is negative x,y multiplied by zoom factor minus half lens size
    const bgX = -x * zoomFactor + lensWidth / 2;
    const bgY = -y * zoomFactor + lensHeight / 2;
    lens.style.backgroundPosition = `${bgX}px ${bgY}px`;
  }

  // Set the background image of the lens and its size
  function setLensBackground() {
    const imgSrc = image.getAttribute('data-src') || image.src;
    lens.style.backgroundImage = `url('${imgSrc}')`;
    // Background size is image natural width * zoom factor by natural height * zoom factor
    const naturalWidth = image.naturalWidth;
    const naturalHeight = image.naturalHeight;
    lens.style.backgroundSize = `${naturalWidth * zoomFactor}px ${naturalHeight * zoomFactor}px`;
  }

  // Calculate distance between two touches for pinch zoom
  function getDistance(touches) {
    const [touch1, touch2] = touches;
    const dx = touch2.clientX - touch1.clientX;
    const dy = touch2.clientY - touch1.clientY;
    return Math.hypot(dx, dy);
  }

  // Event handlers
  function onMouseEnter() {
    setLensBackground();
    showLens();
  }

  function onMouseLeave() {
    hideLens();
  }

  function onMouseMove(e) {
    if (!lensVisible) return;
    const pos = getPos(e);
    moveLens(pos.x, pos.y);
  }

  function onTouchStart(e) {
    if (e.touches.length === 1) {
      setLensBackground();
      showLens();
      const pos = getPos(e);
      moveLens(pos.x, pos.y);
      lastDistance = null;
    } else if (e.touches.length === 2) {
      lastDistance = getDistance(e.touches);
    }
  }

  // Touch move handler
  function onTouchMove(e) {
    if (!lensVisible) return;
    e.preventDefault();
    if (e.touches.length === 1) {
      const pos = getPos(e);
      moveLens(pos.x, pos.y);
    } else if (e.touches.length === 2) {
      // Pinch zoom detection
      const currentDistance = getDistance(e.touches);
      if (lastDistance) {
        const delta = currentDistance - lastDistance;
        // Optional: could implement zoom factor change on pinch here if desired.
        // For now, just keep zoom fixed at 2x as requested.
      }
      lastDistance = currentDistance;
    }
  }

  function onTouchEnd(e) {
    if (e.touches.length === 0) {
      hideLens();
      lastDistance = null;
    }
  }

  // Attach event listeners
  image.addEventListener('mouseenter', onMouseEnter);
  image.addEventListener('mouseleave', onMouseLeave);
  image.addEventListener('mousemove', onMouseMove);

  image.addEventListener('touchstart', onTouchStart, { passive: false });
  image.addEventListener('touchmove', onTouchMove, { passive: false });
  image.addEventListener('touchend', onTouchEnd);
  image.addEventListener('touchcancel', onTouchEnd);

  // Expose setLensBackground globally for the popup initialization
  window.setLensBackground = setLensBackground;
})();
