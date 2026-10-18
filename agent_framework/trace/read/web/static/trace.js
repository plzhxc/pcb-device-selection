/*
 * Agent Tracer frontend
 *
 * This file intentionally has no framework or build-step dependency. Mermaid
 * remains an optional, separately loaded browser dependency: pages without it
 * still expose the escaped diagram source instead of breaking the Trace UI.
 */

(() => {
  "use strict";

  const MIN_DIAGRAM_SCALE = 0.5;
  const MAX_DIAGRAM_SCALE = 6;
  const BUTTON_ZOOM_FACTOR = 1.25;

  const clamp = (value, minimum, maximum) =>
    Math.min(maximum, Math.max(minimum, value));

  const copyViewBox = (viewBox) => ({
    x: viewBox.x,
    y: viewBox.y,
    width: viewBox.width,
    height: viewBox.height,
  });

  const setupDiagramControls = (shell) => {
    const viewport = shell.querySelector("[data-diagram-viewport]");
    const svg = viewport?.querySelector("svg");
    const scaleOutput = shell.querySelector("[data-diagram-scale]");
    const controls = [...shell.querySelectorAll("[data-diagram-action]")];
    if (!viewport || !svg || !scaleOutput || controls.length === 0) {
      return;
    }

    const rendered = svg.viewBox.baseVal;
    if (!rendered || rendered.width <= 0 || rendered.height <= 0) {
      return;
    }

    const state = {
      base: copyViewBox(rendered),
      view: copyViewBox(rendered),
      scale: 1,
      drag: null,
    };

    svg.setAttribute("width", "100%");
    svg.setAttribute("height", "100%");
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

    const constrainView = (view) => {
      const base = state.base;
      const minimumX = Math.min(base.x, base.x + base.width - view.width);
      const maximumX = Math.max(base.x, base.x + base.width - view.width);
      const minimumY = Math.min(base.y, base.y + base.height - view.height);
      const maximumY = Math.max(base.y, base.y + base.height - view.height);
      return {
        ...view,
        x: clamp(view.x, minimumX, maximumX),
        y: clamp(view.y, minimumY, maximumY),
      };
    };

    const render = () => {
      state.view = constrainView(state.view);
      const { x, y, width, height } = state.view;
      svg.setAttribute("viewBox", `${x} ${y} ${width} ${height}`);
      shell.dataset.diagramScale = state.scale.toFixed(3);
      viewport.dataset.diagramViewBox = `${x} ${y} ${width} ${height}`;
      scaleOutput.value = `${Math.round(state.scale * 100)}%`;
      scaleOutput.textContent = scaleOutput.value;
      controls.forEach((button) => {
        button.disabled =
          (button.dataset.diagramAction === "zoom-in" &&
            state.scale >= MAX_DIAGRAM_SCALE) ||
          (button.dataset.diagramAction === "zoom-out" &&
            state.scale <= MIN_DIAGRAM_SCALE);
      });
    };

    const clientPointToSvg = (clientX, clientY) => {
      const matrix = svg.getScreenCTM();
      if (!matrix) {
        return {
          x: state.view.x + state.view.width / 2,
          y: state.view.y + state.view.height / 2,
        };
      }
      const point = svg.createSVGPoint();
      point.x = clientX;
      point.y = clientY;
      return point.matrixTransform(matrix.inverse());
    };

    const zoomTo = (requestedScale, clientPoint = null) => {
      const nextScale = clamp(
        requestedScale,
        MIN_DIAGRAM_SCALE,
        MAX_DIAGRAM_SCALE,
      );
      if (Math.abs(nextScale - state.scale) < 0.001) {
        return;
      }

      const anchor = clientPoint
        ? clientPointToSvg(clientPoint.x, clientPoint.y)
        : {
            x: state.view.x + state.view.width / 2,
            y: state.view.y + state.view.height / 2,
          };
      const anchorX = clamp(
        (anchor.x - state.view.x) / state.view.width,
        0,
        1,
      );
      const anchorY = clamp(
        (anchor.y - state.view.y) / state.view.height,
        0,
        1,
      );
      const width = state.base.width / nextScale;
      const height = state.base.height / nextScale;
      state.view = {
        x: anchor.x - width * anchorX,
        y: anchor.y - height * anchorY,
        width,
        height,
      };
      state.scale = nextScale;
      render();
    };

    const reset = () => {
      state.scale = 1;
      state.view = copyViewBox(state.base);
      render();
    };

    const panBy = (x, y) => {
      state.view = {
        ...state.view,
        x: state.view.x + x,
        y: state.view.y + y,
      };
      render();
    };

    shell.addEventListener("click", (event) => {
      const button = event.target.closest("[data-diagram-action]");
      if (!button || button.disabled) {
        return;
      }
      const action = button.dataset.diagramAction;
      if (action === "zoom-in") {
        zoomTo(state.scale * BUTTON_ZOOM_FACTOR);
      } else if (action === "zoom-out") {
        zoomTo(state.scale / BUTTON_ZOOM_FACTOR);
      } else if (action === "reset") {
        reset();
        viewport.focus({ preventScroll: true });
      }
    });

    viewport.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        const factor = Math.exp(-event.deltaY * 0.0015);
        zoomTo(state.scale * factor, {
          x: event.clientX,
          y: event.clientY,
        });
      },
      { passive: false },
    );

    viewport.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || !event.isPrimary) {
        return;
      }
      const bounds = svg.getBoundingClientRect();
      state.drag = {
        pointerId: event.pointerId,
        clientX: event.clientX,
        clientY: event.clientY,
        view: copyViewBox(state.view),
        unitsPerPixel: Math.max(
          state.view.width / Math.max(bounds.width, 1),
          state.view.height / Math.max(bounds.height, 1),
        ),
      };
      viewport.setPointerCapture(event.pointerId);
      viewport.classList.add("is-dragging");
    });

    viewport.addEventListener("pointermove", (event) => {
      const drag = state.drag;
      if (!drag || drag.pointerId !== event.pointerId) {
        return;
      }
      event.preventDefault();
      state.view = {
        ...drag.view,
        x:
          drag.view.x -
          (event.clientX - drag.clientX) * drag.unitsPerPixel,
        y:
          drag.view.y -
          (event.clientY - drag.clientY) * drag.unitsPerPixel,
      };
      render();
    });

    const endDrag = (event) => {
      if (!state.drag || state.drag.pointerId !== event.pointerId) {
        return;
      }
      if (viewport.hasPointerCapture(event.pointerId)) {
        viewport.releasePointerCapture(event.pointerId);
      }
      state.drag = null;
      viewport.classList.remove("is-dragging");
    };
    viewport.addEventListener("pointerup", endDrag);
    viewport.addEventListener("pointercancel", endDrag);
    viewport.addEventListener("dblclick", reset);

    viewport.addEventListener("keydown", (event) => {
      const horizontalStep = state.view.width * 0.08;
      const verticalStep = state.view.height * 0.08;
      let handled = true;
      if (event.key === "+" || event.key === "=") {
        zoomTo(state.scale * BUTTON_ZOOM_FACTOR);
      } else if (event.key === "-" || event.key === "_") {
        zoomTo(state.scale / BUTTON_ZOOM_FACTOR);
      } else if (event.key === "0" || event.key === "Home") {
        reset();
      } else if (event.key === "ArrowLeft") {
        panBy(-horizontalStep, 0);
      } else if (event.key === "ArrowRight") {
        panBy(horizontalStep, 0);
      } else if (event.key === "ArrowUp") {
        panBy(0, -verticalStep);
      } else if (event.key === "ArrowDown") {
        panBy(0, verticalStep);
      } else {
        handled = false;
      }
      if (handled) {
        event.preventDefault();
      }
    });

    shell.dataset.diagramReady = "true";
    render();
  };

  const renderTraceDiagrams = async () => {
    if (!window.mermaid) {
      return;
    }

    window.mermaid.initialize({
      startOnLoad: false,
      theme: "default",
      securityLevel: "strict",
      flowchart: {
        curve: "basis",
        htmlLabels: false,
      },
    });

    await window.mermaid.run({
      querySelector: ".mermaid",
    });
    document
      .querySelectorAll("[data-trace-diagram]")
      .forEach(setupDiagramControls);
  };

  document.addEventListener("DOMContentLoaded", () => {
    void renderTraceDiagrams();
  });
})();
