"use client";

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";

import type { CadTopologyMesh } from "@/lib/studio-api";
import type { OperationBounds } from "@/lib/emg-tree";

interface Props {
  url: string;
  loadingLabel: string;
  errorLabel: string;
  // Ф3.2: per-face tessellation (content-stable face keys, see
  // _topology_mesh in infra/cad-kernel/server.py). When given and it loads,
  // the model renders as one mesh PER FACE instead of one merged STL mesh —
  // same geometry, same look, but now individually raycastable/highlightable.
  // Absent or failing to load falls back to the plain STL mesh, unchanged.
  topologyUrl?: string;
  // Controlled selection — a parent panel (a feature-tree row, say) can
  // drive the highlight without owning the raycasting itself.
  selectedFaceKey?: string | null;
  onFaceSelect?: (faceKey: string | null) => void;
  // Ф7: same controlled-selection pattern, for edges (fillet/chamfer's
  // edge_key) — independent of face selection, both can be wired at once
  // without conflict (see onClick's own two separate raycasts below).
  selectedEdgeKey?: string | null;
  onEdgeSelect?: (edgeKey: string | null) => void;
  // Ф3.1/B3: kernel-measured per-operation B-Rep delta bounds (mm, model's
  // own frame — see lib/emg-tree.ts's operationBoundsFromFeatureResults),
  // keyed by operation id. Purely additive: undefined/empty draws nothing
  // and changes no existing behaviour.
  operationBounds?: Map<string, OperationBounds>;
  // Operation ids to always outline (e.g. "needs review" from
  // solid_3d.assumptions) — drawn amber, dimmer than the selection.
  flaggedOperationIds?: Set<string>;
  // The tree's current selection — drawn brighter, on top of a flagged
  // outline if both apply.
  selectedOperationId?: string | null;
  // B2: fires when a click's ray hits the model AND the hit point falls
  // inside exactly one (or, on overlap, the smallest) operation bounding
  // box — null when the click missed the model or matched no box. Never a
  // guess: plain axis-aligned containment against measured bounds.
  onOperationClick?: (operationId: string | null) => void;
  // The host div's own height class — defaults to a fixed preview size
  // (CadWorkspace.tsx's read-only review embed relies on this; nothing in
  // its own ancestor chain defines a height for a plain "h-full" to fill).
  // The ribbon editor's Viewport.tsx passes "h-full" instead: its own
  // flex-1 ancestor DOES already give this component a real, fillable
  // height — the fixed size there wasted roughly half the viewport as an
  // indistinguishable dark gap below a small model (live user report:
  // "занято пол экрана... непонятный интерфейс").
  heightClassName?: string;
  // Imperative camera actions are represented as immutable commands so the
  // surrounding editor can provide ordinary accessible buttons without
  // reaching into Three.js objects. `nonce` makes repeated equal actions
  // observable (two consecutive zoom-ins, for example).
  viewCommand?: CadViewCommand;
}

export interface CadViewCommand {
  type: "fit_model" | "fit_selection" | "zoom_in" | "zoom_out";
  nonce: number;
}

const BASE_COLOR = 0xd4d4d8;
const SELECTED_COLOR = 0x38bdf8;
const FLAGGED_COLOR = 0xf59e0b;

export default function CadModelViewer({
  url,
  loadingLabel,
  errorLabel,
  topologyUrl,
  selectedFaceKey,
  onFaceSelect,
  selectedEdgeKey,
  onEdgeSelect,
  operationBounds,
  flaggedOperationIds,
  selectedOperationId,
  onOperationClick,
  heightClassName = "h-[360px] sm:h-[440px]",
  viewCommand,
}: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  // Exposed so the selectedFaceKey effect (a separate hook, driven by a
  // prop that can change without re-loading the model) can reach the live
  // per-face meshes without re-running the whole scene-setup effect.
  const facesRef = useRef<Map<string, THREE.Mesh> | null>(null);
  // Ф7: same reason, for per-edge lines (fillet/chamfer edge_key).
  const edgesRef = useRef<Map<string, THREE.Line> | null>(null);
  // Same reason, for the merged-mesh fallback: click hit-testing needs a
  // raycast target even when no topology loaded (operation bounds don't
  // need per-face granularity).
  const mergedMeshRef = useRef<THREE.Mesh | null>(null);
  // The centering translation the load effect applies to everything in the
  // scene — operation bounds arrive in the model's ORIGINAL (uncentered) mm
  // frame and must be shifted the same way to land on the visible geometry.
  const centerRef = useRef(new THREE.Vector3());
  const boundsGroupRef = useRef<THREE.Group | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const modelBoxRef = useRef<THREE.Box3 | null>(null);
  const viewCommandRef = useRef(viewCommand);
  viewCommandRef.current = viewCommand;
  // Every operation's box in SCENE (centered) coordinates, rebuilt whenever
  // the overlay redraws — click hit-testing reuses these, not just the ones
  // actually drawn as amber/selected outlines.
  const hitBoxesRef = useRef<Map<string, THREE.Box3>>(new Map());
  // Latest-value ref for props the click handler and the imperative overlay
  // redraw need without re-running (and re-fetching) the whole load effect.
  const overlayRef = useRef({
    operationBounds,
    flaggedOperationIds,
    selectedOperationId,
    onOperationClick,
  });
  overlayRef.current = {
    operationBounds,
    flaggedOperationIds,
    selectedOperationId,
    onOperationClick,
  };

  const applyViewCommand = (command: CadViewCommand | undefined) => {
    if (!command) return;
    const camera = cameraRef.current;
    const controls = controlsRef.current;
    if (!camera || !controls) return;
    if (command.type === "zoom_in" || command.type === "zoom_out") {
      const factor = command.type === "zoom_in" ? 0.75 : 4 / 3;
      const offset = camera.position.clone().sub(controls.target);
      camera.position.copy(controls.target).addScaledVector(offset, factor);
      controls.update();
      return;
    }
    const selectedId = overlayRef.current.selectedOperationId;
    const targetBox =
      command.type === "fit_selection" && selectedId
        ? hitBoxesRef.current.get(selectedId)
        : modelBoxRef.current;
    if (!targetBox || targetBox.isEmpty()) return;
    const center = targetBox.getCenter(new THREE.Vector3());
    const size = targetBox.getSize(new THREE.Vector3());
    const radius = Math.max(size.length() / 2, 0.001);
    const direction = camera.position.clone().sub(controls.target).normalize();
    if (direction.lengthSq() === 0) direction.set(1, -1, 0.75).normalize();
    const verticalFov = THREE.MathUtils.degToRad(camera.fov);
    const horizontalFov = 2 * Math.atan(Math.tan(verticalFov / 2) * camera.aspect);
    const limitingFov = Math.max(0.01, Math.min(verticalFov, horizontalFov));
    const distance = (radius / Math.sin(limitingFov / 2)) * 1.15;
    controls.target.copy(center);
    camera.position.copy(center).addScaledVector(direction, distance);
    controls.update();
  };

  // Clears and repopulates the outline overlay from overlayRef's current
  // values — called once the model's center is known (on load) and again
  // whenever the overlay-relevant props change (the effect below), never
  // reloading the model itself.
  const syncBoundsOverlay = () => {
    const group = boundsGroupRef.current;
    if (!group) return;
    while (group.children.length) {
      const child = group.children[group.children.length - 1];
      group.remove(child);
      if (child instanceof THREE.LineSegments) {
        child.geometry.dispose();
        const material = child.material as THREE.Material;
        material.dispose();
      }
    }
    const {
      operationBounds: bounds,
      flaggedOperationIds: flagged,
      selectedOperationId: selected,
    } = overlayRef.current;
    const boxes = new Map<string, THREE.Box3>();
    const center = centerRef.current;
    bounds?.forEach((box, id) => {
      const box3 = new THREE.Box3(
        new THREE.Vector3(
          box.x_min - center.x,
          box.y_min - center.y,
          box.z_min - center.z,
        ),
        new THREE.Vector3(
          box.x_max - center.x,
          box.y_max - center.y,
          box.z_max - center.z,
        ),
      );
      boxes.set(id, box3);
      const isSelected = id === selected;
      const isFlagged = flagged?.has(id) ?? false;
      if (!isSelected && !isFlagged) return;
      const helper = new THREE.Box3Helper(
        box3,
        new THREE.Color(isSelected ? SELECTED_COLOR : FLAGGED_COLOR),
      );
      const material = helper.material as THREE.LineBasicMaterial;
      material.transparent = true;
      material.opacity = isSelected ? 0.95 : 0.55;
      group.add(helper);
    });
    hitBoxesRef.current = boxes;
  };

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    setState("loading");
    facesRef.current = null;
    edgesRef.current = null;
    mergedMeshRef.current = null;
    centerRef.current.set(0, 0, 0);
    hitBoxesRef.current = new Map();

    let frame = 0;
    let disposed = false;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x18181b);
    const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 100000);
    cameraRef.current = camera;
    camera.up.set(0, 0, 1);
    const renderer = new THREE.WebGLRenderer({
      antialias: true,
      powerPreference: "high-performance",
      preserveDrawingBuffer: true,
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.shadowMap.enabled = true;
    renderer.domElement.dataset.testid = "cad-3d-canvas";
    host.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controlsRef.current = controls;
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.screenSpacePanning = true;

    scene.add(new THREE.HemisphereLight(0xf4f4f5, 0x27272a, 2.4));
    const key = new THREE.DirectionalLight(0xffffff, 3.2);
    key.position.set(4, -3, 6);
    key.castShadow = true;
    scene.add(key);
    const fill = new THREE.DirectionalLight(0xffe8c7, 1.2);
    fill.position.set(-4, 2, 1);
    scene.add(fill);

    const resize = () => {
      const width = Math.max(1, host.clientWidth);
      const height = Math.max(1, host.clientHeight);
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    resize();

    // The outline overlay lives for the whole scene lifetime; syncBoundsOverlay
    // (re)populates it once the center is known and again on prop changes.
    const boundsGroup = new THREE.Group();
    scene.add(boundsGroup);
    boundsGroupRef.current = boundsGroup;

    // Ф3.2/B2: click → raycast against the per-face meshes (when topology
    // loaded, else the merged mesh) → resolve a stable face_key AND/OR,
    // via the SAME hit point, whichever operation's measured bounds box
    // contains it (smallest volume wins on overlap — never a guess).
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const onClick = (event: PointerEvent) => {
      const { onOperationClick } = overlayRef.current;
      if (!onFaceSelect && !onOperationClick && !onEdgeSelect) return;
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      // Ф7: a fully independent raycast against edge lines only — never
      // shares a hit with the face/operation resolution below, so wiring
      // onEdgeSelect changes nothing about existing face/operation click
      // behaviour when it isn't passed.
      if (onEdgeSelect) {
        const edgeTargets = edgesRef.current
          ? Array.from(edgesRef.current.values())
          : [];
        const edgeHit = edgeTargets.length
          ? raycaster.intersectObjects(edgeTargets, false)[0]
          : undefined;
        onEdgeSelect(
          edgeHit
            ? ((edgeHit.object as THREE.Line).userData.edgeKey as string)
            : null,
        );
      }
      const targets: THREE.Object3D[] = facesRef.current
        ? Array.from(facesRef.current.values())
        : mergedMeshRef.current
          ? [mergedMeshRef.current]
          : [];
      const hit = raycaster.intersectObjects(targets, false)[0];
      if (onFaceSelect) {
        const hitKey =
          hit && facesRef.current
            ? ((hit.object as THREE.Mesh).userData.faceKey as string)
            : null;
        onFaceSelect(hitKey);
      }
      if (onOperationClick) {
        let bestId: string | null = null;
        let bestVolume = Infinity;
        if (hit) {
          hitBoxesRef.current.forEach((box3, id) => {
            if (!box3.containsPoint(hit.point)) return;
            const size = box3.getSize(new THREE.Vector3());
            const volume = size.x * size.y * size.z;
            if (volume < bestVolume) {
              bestVolume = volume;
              bestId = id;
            }
          });
        }
        onOperationClick(bestId);
      }
    };
    renderer.domElement.addEventListener("click", onClick);

    new STLLoader().load(
      url,
      (geometry) => {
        if (disposed) {
          geometry.dispose();
          return;
        }
        geometry.computeVertexNormals();
        geometry.computeBoundingBox();
        const bounds = geometry.boundingBox;
        if (!bounds) {
          setState("error");
          return;
        }
        const center = bounds.getCenter(new THREE.Vector3());
        centerRef.current.copy(center);
        modelBoxRef.current = new THREE.Box3(
          bounds.min.clone().sub(center),
          bounds.max.clone().sub(center),
        );

        const edges = new THREE.LineSegments(
          new THREE.EdgesGeometry(geometry, 25),
          new THREE.LineBasicMaterial({
            color: 0x3f3f46,
            transparent: true,
            opacity: 0.72,
          }),
        );
        edges.position.set(-center.x, -center.y, -center.z);
        scene.add(edges);

        // The merged STL mesh — the default visible surface, and the ONLY
        // one when no topology is given or it fails to load.
        const mergedMaterial = new THREE.MeshStandardMaterial({
          color: BASE_COLOR,
          metalness: 0.18,
          roughness: 0.52,
          side: THREE.DoubleSide,
        });
        const mergedGeometry = geometry.clone();
        mergedGeometry.translate(-center.x, -center.y, -center.z);
        const mergedMesh = new THREE.Mesh(mergedGeometry, mergedMaterial);
        mergedMesh.castShadow = true;
        mergedMesh.receiveShadow = true;
        scene.add(mergedMesh);
        mergedMeshRef.current = mergedMesh;

        mergedGeometry.computeBoundingSphere();
        const radius = Math.max(
          mergedGeometry.boundingSphere?.radius ?? 1,
          0.001,
        );
        // Ф7: THREE.Raycaster's default Line threshold is ~0 — a click
        // essentially never lands on a mathematically thin line. Scaled to
        // the model's own size so a click near an edge registers regardless
        // of how large or tiny the part is.
        raycaster.params.Line = { threshold: radius * 0.01 };
        const grid = new THREE.GridHelper(radius * 4, 20, 0x52525b, 0x27272a);
        grid.rotation.x = Math.PI / 2;
        grid.position.z =
          -bounds.getSize(new THREE.Vector3()).z / 2 - radius * 0.01;
        scene.add(grid);

        camera.near = radius / 100;
        camera.far = radius * 100;
        camera.position.set(radius * 1.8, -radius * 2.1, radius * 1.55);
        camera.updateProjectionMatrix();
        controls.target.set(0, 0, 0);
        controls.update();
        setState("ready");
        syncBoundsOverlay();
        // A command can arrive while the STL is still loading. Replaying the
        // latest ref here avoids losing that click to the async load window.
        applyViewCommand(viewCommandRef.current);

        if (!topologyUrl) return;
        fetch(topologyUrl, { credentials: "include" })
          .then((res) =>
            res.ok ? (res.json() as Promise<CadTopologyMesh>) : null,
          )
          .then((mesh) => {
            if (
              disposed ||
              !mesh ||
              !Array.isArray(mesh.faces) ||
              mesh.faces.length === 0
            )
              return;
            const faces = new Map<string, THREE.Mesh>();
            for (const face of mesh.faces) {
              if (!face.vertices?.length || !face.triangles?.length) continue;
              const positions = new Float32Array(face.vertices.length * 3);
              face.vertices.forEach(([x, y, z], i) => {
                positions[i * 3] = x - center.x;
                positions[i * 3 + 1] = y - center.y;
                positions[i * 3 + 2] = z - center.z;
              });
              const faceGeometry = new THREE.BufferGeometry();
              faceGeometry.setAttribute(
                "position",
                new THREE.BufferAttribute(positions, 3),
              );
              faceGeometry.setIndex(face.triangles.flat());
              faceGeometry.computeVertexNormals();
              const faceMesh = new THREE.Mesh(
                faceGeometry,
                new THREE.MeshStandardMaterial({
                  color: BASE_COLOR,
                  metalness: 0.18,
                  roughness: 0.52,
                  side: THREE.DoubleSide,
                }),
              );
              faceMesh.userData.faceKey = face.key;
              faceMesh.castShadow = true;
              faceMesh.receiveShadow = true;
              faceMesh.visible = false; // shown once the whole set is ready
              scene.add(faceMesh);
              faces.set(face.key, faceMesh);
            }
            // Ф7: per-edge lines — independent of the face swap below (an
            // edge is drawn regardless of whether the per-face set ends up
            // used), same visual weight as the cosmetic wireframe they
            // replace so nothing looks different until a click highlights
            // one.
            const edgeLines = new Map<string, THREE.Line>();
            for (const edgeItem of mesh.edges ?? []) {
              if (!edgeItem.polyline || edgeItem.polyline.length < 2) continue;
              const positions = new Float32Array(edgeItem.polyline.length * 3);
              edgeItem.polyline.forEach(([x, y, z], i) => {
                positions[i * 3] = x - center.x;
                positions[i * 3 + 1] = y - center.y;
                positions[i * 3 + 2] = z - center.z;
              });
              const edgeGeometry = new THREE.BufferGeometry();
              edgeGeometry.setAttribute(
                "position",
                new THREE.BufferAttribute(positions, 3),
              );
              const edgeLine = new THREE.Line(
                edgeGeometry,
                new THREE.LineBasicMaterial({
                  color: 0x3f3f46,
                  transparent: true,
                  opacity: 0.72,
                }),
              );
              edgeLine.userData.edgeKey = edgeItem.key;
              scene.add(edgeLine);
              edgeLines.set(edgeItem.key, edgeLine);
            }
            if (!disposed && edgeLines.size > 0) {
              // The cosmetic silhouette (built from the merged STL, no
              // keys) would otherwise draw every one of these same edges a
              // second time, indistinguishably — hide it now that the real,
              // individually-keyed lines are up.
              edges.visible = false;
              edgesRef.current = edgeLines;
            }
            if (disposed || faces.size === 0) return;
            // Swap: hide the merged mesh, show the per-face set — same
            // surface, now individually selectable.
            mergedMesh.visible = false;
            faces.forEach((faceMesh) => {
              faceMesh.visible = true;
            });
            facesRef.current = faces;
            // Test hook: the async topology fetch/swap above has no other
            // externally observable signal — this lets an e2e test wait
            // for raycasting to actually be live before clicking.
            renderer.domElement.dataset.topologyReady = "true";
          })
          .catch(() => {
            // Topology is a best-effort enhancement — its absence keeps the
            // plain merged STL mesh already on screen, not an error state.
          });
      },
      undefined,
      () => !disposed && setState("error"),
    );

    const animate = () => {
      controls.update();
      renderer.render(scene, camera);
      frame = window.requestAnimationFrame(animate);
    };
    animate();

    return () => {
      disposed = true;
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      renderer.domElement.removeEventListener("click", onClick);
      controls.dispose();
      scene.traverse((object) => {
        if (
          object instanceof THREE.Mesh ||
          object instanceof THREE.LineSegments ||
          object instanceof THREE.Line
        ) {
          object.geometry.dispose();
          const materials = Array.isArray(object.material)
            ? object.material
            : [object.material];
          materials.forEach((material) => material.dispose());
        }
      });
      renderer.dispose();
      renderer.domElement.remove();
      boundsGroupRef.current = null;
      cameraRef.current = null;
      controlsRef.current = null;
      modelBoxRef.current = null;
    };
  }, [url, topologyUrl, onFaceSelect, onEdgeSelect]);

  useEffect(() => {
    applyViewCommand(viewCommand);
  }, [viewCommand]);

  // Controlled highlight — separate from the load effect so changing the
  // selection never re-fetches or re-builds the scene.
  useEffect(() => {
    const faces = facesRef.current;
    if (!faces) return;
    faces.forEach((mesh, key) => {
      const material = mesh.material as THREE.MeshStandardMaterial;
      material.color.set(key === selectedFaceKey ? SELECTED_COLOR : BASE_COLOR);
    });
  }, [selectedFaceKey]);

  // Ф7: same controlled-highlight treatment for the selected edge.
  useEffect(() => {
    const edgeLines = edgesRef.current;
    if (!edgeLines) return;
    edgeLines.forEach((line, key) => {
      const material = line.material as THREE.LineBasicMaterial;
      const isSelected = key === selectedEdgeKey;
      material.color.set(isSelected ? SELECTED_COLOR : 0x3f3f46);
      material.opacity = isSelected ? 1 : 0.72;
    });
  }, [selectedEdgeKey]);

  // B3: same "controlled, no reload" treatment for the operation-bounds
  // overlay — a tree-row click or a new assumptions list must not re-fetch
  // the model.
  useEffect(() => {
    // syncBoundsOverlay reads overlayRef.current (kept fresh every render)
    // rather than these values directly — listed here only to trigger the
    // redraw at the right time.
    syncBoundsOverlay();
  }, [operationBounds, flaggedOperationIds, selectedOperationId]);

  return (
    <div
      ref={hostRef}
      className={`relative w-full overflow-hidden bg-zinc-900 ${heightClassName}`}
    >
      {state !== "ready" && (
        <div
          className={`pointer-events-none absolute inset-0 z-10 grid place-items-center text-sm ${state === "error" ? "text-red-400" : "text-zinc-400"}`}
        >
          {state === "error" ? errorLabel : loadingLabel}
        </div>
      )}
    </div>
  );
}
