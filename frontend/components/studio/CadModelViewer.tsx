"use client";

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";

import type { CadTopologyMesh } from "@/lib/studio-api";

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
}

const BASE_COLOR = 0xd4d4d8;
const SELECTED_COLOR = 0x38bdf8;

export default function CadModelViewer({
  url,
  loadingLabel,
  errorLabel,
  topologyUrl,
  selectedFaceKey,
  onFaceSelect,
}: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  // Exposed so the selectedFaceKey effect (a separate hook, driven by a
  // prop that can change without re-loading the model) can reach the live
  // per-face meshes without re-running the whole scene-setup effect.
  const facesRef = useRef<Map<string, THREE.Mesh> | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    setState("loading");
    facesRef.current = null;

    let frame = 0;
    let disposed = false;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x18181b);
    const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 100000);
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

    // Ф3.2: click → raycast against the per-face meshes (when topology
    // loaded) → resolve to a stable face_key, never a raw mesh index.
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const onClick = (event: PointerEvent) => {
      if (!facesRef.current || !onFaceSelect) return;
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const meshes = Array.from(facesRef.current.values());
      const hit = raycaster.intersectObjects(meshes, false)[0];
      const hitKey = hit
        ? ((hit.object as THREE.Mesh).userData.faceKey as string)
        : null;
      onFaceSelect(hitKey);
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

        mergedGeometry.computeBoundingSphere();
        const radius = Math.max(
          mergedGeometry.boundingSphere?.radius ?? 1,
          0.001,
        );
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
          object instanceof THREE.LineSegments
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
    };
  }, [url, topologyUrl, onFaceSelect]);

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

  return (
    <div
      ref={hostRef}
      className="relative h-[360px] w-full overflow-hidden bg-zinc-900 sm:h-[440px]"
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
