"use client";

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";

interface Props {
  url: string;
  loadingLabel: string;
  errorLabel: string;
}

export default function CadModelViewer({ url, loadingLabel, errorLabel }: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    setState("loading");

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
        geometry.translate(-center.x, -center.y, -center.z);
        geometry.computeBoundingSphere();
        const radius = Math.max(geometry.boundingSphere?.radius ?? 1, 0.001);
        const material = new THREE.MeshStandardMaterial({
          color: 0xd4d4d8,
          metalness: 0.18,
          roughness: 0.52,
          side: THREE.DoubleSide,
        });
        const mesh = new THREE.Mesh(geometry, material);
        mesh.castShadow = true;
        mesh.receiveShadow = true;
        scene.add(mesh);

        const edges = new THREE.LineSegments(
          new THREE.EdgesGeometry(geometry, 25),
          new THREE.LineBasicMaterial({ color: 0x3f3f46, transparent: true, opacity: 0.72 }),
        );
        scene.add(edges);
        const grid = new THREE.GridHelper(radius * 4, 20, 0x52525b, 0x27272a);
        grid.rotation.x = Math.PI / 2;
        grid.position.z = -bounds.getSize(new THREE.Vector3()).z / 2 - radius * 0.01;
        scene.add(grid);

        camera.near = radius / 100;
        camera.far = radius * 100;
        camera.position.set(radius * 1.8, -radius * 2.1, radius * 1.55);
        camera.updateProjectionMatrix();
        controls.target.set(0, 0, 0);
        controls.update();
        setState("ready");
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
      controls.dispose();
      scene.traverse((object) => {
        if (object instanceof THREE.Mesh || object instanceof THREE.LineSegments) {
          object.geometry.dispose();
          const materials = Array.isArray(object.material) ? object.material : [object.material];
          materials.forEach((material) => material.dispose());
        }
      });
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, [url]);

  return (
    <div ref={hostRef} className="relative h-[360px] w-full overflow-hidden bg-zinc-900 sm:h-[440px]">
      {state !== "ready" && (
        <div className={`pointer-events-none absolute inset-0 z-10 grid place-items-center text-sm ${state === "error" ? "text-red-400" : "text-zinc-400"}`}>
          {state === "error" ? errorLabel : loadingLabel}
        </div>
      )}
    </div>
  );
}
