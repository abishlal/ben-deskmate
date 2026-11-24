// Ben, the desktop pet's original 3D character: a chibi kid with chestnut hair, green eyes, an olive zip jacket
// over a white tee, black trousers, sneakers and a headset whose visor glows green when he listens.
// Built from simple rounded shapes with soft lighting, so it needs no model files.
import * as THREE from 'three';

const C = {
  skin: 0xe9b890, cheek: 0xf0a58a, hair: 0x6e3f22, hairDark: 0x55301a, brow: 0x5a321b,
  eyeWhite: 0xffffff, iris: 0x3e9e4c, pupil: 0x1d1a17, mouth: 0x8c3a2c, tongue: 0xd9665a,
  jacket: 0x3f5240, jacketDark: 0x2f3e31, tee: 0xf5f5f2, trousers: 0x26272b, shoe: 0x1c1c1f, sole: 0xf3f3f0,
  headset: 0x1f2124, visorGlow: 0x4fe060, visorQuiet: 0xf2b632, zip: 0xc9c9c4,
};

function mat(color, opts = {}) {
  return new THREE.MeshStandardMaterial({ color, roughness: 0.55, metalness: 0.0, ...opts });
}
function mesh(geo, material, { x = 0, y = 0, z = 0, sx = 1, sy = 1, sz = 1, rx = 0, ry = 0, rz = 0, shadow = true } = {}) {
  const m = new THREE.Mesh(geo, material);
  m.position.set(x, y, z); m.scale.set(sx, sy, sz); m.rotation.set(rx, ry, rz);
  m.castShadow = shadow; m.receiveShadow = false;
  return m;
}
let detail = 1;   // mesh detail; lite mode halves it (he is only ~150 px tall in the pet window)
const sphere = (r = 1, seg = 40) => new THREE.SphereGeometry(r, Math.max(8, Math.round(seg * detail)), Math.max(6, Math.round(seg * 0.75 * detail)));
const capsule = (r, len) => new THREE.CapsuleGeometry(r, len, Math.round(10 * detail), Math.round(24 * detail));

function softShadowTexture() {
  const c = document.createElement('canvas'); c.width = c.height = 128;
  const g = c.getContext('2d'); const grad = g.createRadialGradient(64, 64, 4, 64, 64, 62);
  grad.addColorStop(0, 'rgba(0,0,0,0.35)'); grad.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = grad; g.fillRect(0, 0, 128, 128);
  return new THREE.CanvasTexture(c);
}

function textTexture(text, color) {
  const c = document.createElement('canvas'); c.width = 256; c.height = 128;
  const g = c.getContext('2d');
  g.font = 'bold 96px Consolas, monospace'; g.textAlign = 'center'; g.textBaseline = 'middle';
  g.fillStyle = color; g.fillText(text, 128, 68);
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

function codeTexture() {
  const c = document.createElement('canvas'); c.width = 256; c.height = 160;
  const g = c.getContext('2d');
  g.fillStyle = '#10161a'; g.fillRect(0, 0, 256, 160);
  const colors = ['#8df59a', '#7cc7ff', '#e8e6df', '#f2b632'];
  for (let row = 0; row < 9; row++) {
    let x = 14 + (row % 3) * 14;
    for (let i = 0; i < 3 && x < 220; i++) {
      const w = 22 + ((row * 37 + i * 53) % 60);
      g.fillStyle = colors[(row + i) % colors.length]; g.fillRect(x, 14 + row * 15, w, 6); x += w + 8;
    }
  }
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

function checklistTexture() {
  const c = document.createElement('canvas'); c.width = 128; c.height = 160;
  const g = c.getContext('2d');
  g.fillStyle = '#fbfaf5'; g.fillRect(0, 0, 128, 160);
  g.lineWidth = 5; g.lineCap = 'round'; g.lineJoin = 'round';
  for (let row = 0; row < 4; row++) {
    const y = 30 + row * 34;
    g.strokeStyle = '#8a8f96'; g.strokeRect(12, y - 10, 20, 20);
    g.fillStyle = '#c3c7cc'; g.fillRect(42, y - 4, 70 - (row % 2) * 18, 8);
    if (row < 2) { g.strokeStyle = '#2f9b45'; g.beginPath(); g.moveTo(14, y); g.lineTo(21, y + 8); g.lineTo(34, y - 12); g.stroke(); }
  }
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

function buildBen() {
  const root = new THREE.Group();          // stands on y = 0
  const body = new THREE.Group(); root.add(body);

  // ---- legs and shoes
  const legMat = mat(C.trousers, { roughness: 0.8 });
  const legs = [];
  for (const side of [-1, 1]) {
    const hip = new THREE.Group(); hip.position.set(side * 0.24, 1.05, 0); body.add(hip);
    hip.add(mesh(capsule(0.2, 0.55), legMat, { y: -0.45 }));
    const shoe = new THREE.Group(); shoe.position.set(0, -0.9, 0.06); hip.add(shoe);
    shoe.add(mesh(sphere(1), mat(C.shoe, { roughness: 0.45 }), { sx: 0.24, sy: 0.15, sz: 0.36, y: 0.02, z: 0.04 }));
    shoe.add(mesh(sphere(1), mat(C.sole, { roughness: 0.6 }), { sx: 0.25, sy: 0.07, sz: 0.37, y: -0.07, z: 0.04 }));
    shoe.add(mesh(new THREE.TorusGeometry(0.1, 0.02, 8, 24), mat(C.sole), { y: 0.1, z: 0.22, rx: Math.PI / 2.6, sx: 1.2 }));
    legs.push(hip);
  }

  // ---- torso: olive jacket over a white tee
  const torso = new THREE.Group(); torso.position.y = 1.55; body.add(torso);
  const jacketMat = mat(C.jacket, { roughness: 0.75 });
  torso.add(mesh(capsule(0.48, 0.42), jacketMat, { sx: 1.05, sz: 0.82 }));
  // a coder's hoodie: hood resting behind the neck, a sliver of white tee, drawstrings, a front pocket, a "</>" print
  const darkMat = mat(C.jacketDark, { roughness: 0.85 });
  torso.add(mesh(sphere(1), darkMat, { sx: 0.5, sy: 0.3, sz: 0.3, y: 0.5, z: -0.3 }));                          // hood
  torso.add(mesh(new THREE.TorusGeometry(0.2, 0.07, 12, 32), darkMat, { y: 0.52, z: 0.04, rx: Math.PI / 2.3 })); // collar
  torso.add(mesh(sphere(1), mat(C.tee, { roughness: 0.7 }), { sx: 0.13, sy: 0.05, sz: 0.08, y: 0.5, z: 0.2 }));
  for (const side of [-1, 1]) {
    torso.add(mesh(new THREE.CylinderGeometry(0.014, 0.014, 0.3, 8), mat(C.tee), { x: side * 0.09, y: 0.3, z: 0.4, rz: side * 0.08 }));
    torso.add(mesh(sphere(1, 12), mat(C.tee), { sx: 0.022, sy: 0.035, sz: 0.022, x: side * 0.102, y: 0.14, z: 0.405 }));
  }
  torso.add(mesh(sphere(1), darkMat, { sx: 0.34, sy: 0.15, sz: 0.08, y: -0.24, z: 0.36 }));                       // front pocket
  const logo = new THREE.Mesh(new THREE.PlaneGeometry(0.34, 0.17), new THREE.MeshBasicMaterial({ map: textTexture('</>', '#8df59a'), transparent: true }));
  logo.position.set(0, 0.06, 0.412); torso.add(logo);
  torso.add(mesh(new THREE.TorusGeometry(0.44, 0.06, 12, 40), mat(C.jacketDark), { y: -0.42, rx: Math.PI / 2, sz: 1, sy: 0.82 }));

  // ---- arms (pivot at the shoulder) with white cuffs and hands
  const skinMat = mat(C.skin, { roughness: 0.6 });
  const arms = {};
  for (const [name, side] of [['left', -1], ['right', 1]]) {
    const shoulder = new THREE.Group(); shoulder.position.set(side * 0.55, 0.3, 0); torso.add(shoulder);
    const upper = new THREE.Group(); shoulder.add(upper);
    upper.add(mesh(capsule(0.15, 0.32), jacketMat, { y: -0.25 }));
    const elbow = new THREE.Group(); elbow.position.y = -0.48; upper.add(elbow);
    elbow.add(mesh(capsule(0.135, 0.2), jacketMat, { y: -0.14 }));
    elbow.add(mesh(new THREE.TorusGeometry(0.13, 0.04, 10, 24), mat(C.jacketDark, { roughness: 0.85 }), { y: -0.3, rx: Math.PI / 2 }));
    const hand = mesh(sphere(1), skinMat, { sx: 0.13, sy: 0.14, sz: 0.12, y: -0.42 });
    elbow.add(hand);
    upper.rotation.z = side * 0.12;
    arms[name] = { shoulder, upper, elbow, hand, side };
  }

  // ---- props held in the hands. Each hand has a holder that pose() keeps lined up with the body, so a glass
  // stays level whatever the arm is doing. Props are oversized: he is only ~150 px tall on screen.
  const holders = {};
  for (const name of ['left', 'right']) {
    const h = new THREE.Group(); h.position.y = -0.42; arms[name].elbow.add(h); holders[name] = h;
  }
  const props = {};
  const glassMat = new THREE.MeshStandardMaterial({ color: 0xeaf7ff, transparent: true, opacity: 0.38, roughness: 0.05, metalness: 0.1, side: THREE.DoubleSide, depthWrite: false });
  const glass = new THREE.Group(); glass.position.set(-0.04, 0.1, 0.16);
  const water = mesh(new THREE.CylinderGeometry(0.15, 0.13, 0.3, 24), new THREE.MeshStandardMaterial({ color: 0x3f9fea, transparent: true, opacity: 0.85, roughness: 0.15, emissive: 0x1d5f99, emissiveIntensity: 0.3 }), { y: -0.06, shadow: false });
  glass.add(water);
  glass.add(mesh(new THREE.CylinderGeometry(0.17, 0.14, 0.44, 24, 1, true), glassMat, { shadow: false }));
  glass.add(mesh(new THREE.CircleGeometry(0.14, 24), glassMat, { y: -0.22, rx: -Math.PI / 2, shadow: false }));
  glass.add(mesh(new THREE.TorusGeometry(0.17, 0.014, 6, 24), mat(0xffffff, { roughness: 0.1 }), { y: 0.22, rx: Math.PI / 2, shadow: false }));
  holders.right.add(glass); props.glass = glass;

  const thumb = mesh(capsule(0.05, 0.1), skinMat, { x: 0.02, y: 0.15, z: 0.02 });
  holders.right.add(thumb); props.thumb = thumb;

  const pencil = new THREE.Group(); pencil.position.set(-0.04, 0.02, 0.1); pencil.rotation.set(-0.7, 0, 0.6);
  pencil.add(mesh(new THREE.CylinderGeometry(0.035, 0.035, 0.36, 8), mat(0xf2c230)));
  pencil.add(mesh(new THREE.ConeGeometry(0.035, 0.08, 8), mat(0x3a2f28), { y: -0.22, rx: Math.PI }));
  pencil.add(mesh(new THREE.CylinderGeometry(0.036, 0.036, 0.05, 8), mat(0xe58a9a), { y: 0.2 }));
  holders.right.add(pencil); props.pencil = pencil;

  const clipboard = new THREE.Group(); clipboard.position.set(0.2, 0.22, 0.22); clipboard.rotation.set(-0.2, 0.15, -0.08);
  clipboard.add(mesh(new THREE.BoxGeometry(0.62, 0.8, 0.04), mat(0xa8774a, { roughness: 0.7 })));
  const paper = new THREE.Mesh(new THREE.PlaneGeometry(0.54, 0.66), new THREE.MeshBasicMaterial({ map: checklistTexture() }));
  paper.position.set(0, -0.04, 0.022); clipboard.add(paper);
  clipboard.add(mesh(new THREE.BoxGeometry(0.26, 0.09, 0.06), mat(0xc9c9c4, { metalness: 0.6, roughness: 0.3 }), { y: 0.38, z: 0.02 }));
  holders.left.add(clipboard); props.clipboard = clipboard;

  const mugMat = mat(0xd9643a, { roughness: 0.5 });
  const mug = new THREE.Group(); mug.position.set(0.06, 0.1, 0.16);
  mug.add(mesh(new THREE.CylinderGeometry(0.16, 0.15, 0.3, 24), mugMat));
  mug.add(mesh(new THREE.CircleGeometry(0.145, 24), mat(0x4a2a17), { y: 0.151, rx: -Math.PI / 2, shadow: false }));
  mug.add(mesh(new THREE.TorusGeometry(0.08, 0.028, 8, 16), mugMat, { x: -0.18 }));
  const steam = [];
  for (let i = 0; i < 2; i++) {
    const puff = mesh(sphere(1, 10), new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0, depthWrite: false }), { sx: 0.07, sy: 0.1, sz: 0.07, shadow: false });
    mug.add(puff); steam.push(puff);
  }
  holders.left.add(mug); props.mug = mug;

  // a watch on his left wrist, for "it's time"
  const watch = new THREE.Group(); watch.position.y = -0.27; arms.left.elbow.add(watch);
  watch.add(mesh(new THREE.TorusGeometry(0.145, 0.045, 8, 20), mat(0xe2b13c, { metalness: 0.5, roughness: 0.3 }), { rx: Math.PI / 2 }));
  for (const z of [-0.17, 0.17]) watch.add(mesh(new THREE.CylinderGeometry(0.11, 0.11, 0.05, 16), mat(0xffffff, { emissive: 0xbfe6ff, emissiveIntensity: 0.4 }), { z, rx: Math.PI / 2 }));
  props.watch = watch;
  for (const p of Object.values(props)) { p.scale.setScalar(0.001); p.visible = false; }

  // ---- head
  const neck = mesh(new THREE.CylinderGeometry(0.13, 0.15, 0.2, 16), skinMat, { y: 2.12 });
  body.add(neck);
  // a laptop that appears in front of him while he works on an answer
  const laptop = new THREE.Group(); laptop.position.set(0, 1.38, 0.8); laptop.scale.setScalar(0.001); body.add(laptop);
  const shell = mat(0xb9bcc2, { metalness: 0.7, roughness: 0.3 });
  laptop.add(mesh(new THREE.BoxGeometry(0.78, 0.035, 0.5), shell));
  const lid = new THREE.Group(); lid.position.set(0, 0.02, 0.25); lid.rotation.x = 0.35; laptop.add(lid);
  lid.add(mesh(new THREE.BoxGeometry(0.78, 0.5, 0.03), shell, { y: 0.25 }));
  const screen = new THREE.Mesh(new THREE.PlaneGeometry(0.7, 0.42), new THREE.MeshBasicMaterial({ map: codeTexture() }));
  screen.position.set(0, 0.25, -0.017); screen.rotation.y = Math.PI; lid.add(screen);

  const head = new THREE.Group(); head.position.y = 2.8; head.scale.setScalar(0.88); body.add(head);
  head.add(mesh(sphere(0.78, 64), skinMat, { sx: 1, sy: 0.96, sz: 0.94 }));
  for (const side of [-1, 1]) {
    head.add(mesh(sphere(1), skinMat, { sx: 0.13, sy: 0.18, sz: 0.09, x: side * 0.76, y: -0.04 }));
    head.add(mesh(sphere(1), mat(C.cheek, { roughness: 0.9, transparent: true, opacity: 0.45 }), { sx: 0.13, sy: 0.07, sz: 0.04, x: side * 0.4, y: -0.22, z: 0.63, shadow: false }));
  }
  head.add(mesh(sphere(1), skinMat, { sx: 0.07, sy: 0.06, sz: 0.06, y: -0.08, z: 0.74 }));   // nose

  // eyes: big and green, with a highlight; blink by scaling the eye group
  const eyes = [];
  for (const side of [-1, 1]) {
    const eye = new THREE.Group(); eye.position.set(side * 0.27, 0.06, 0.62); eye.rotation.y = side * 0.22; head.add(eye);
    eye.add(mesh(sphere(1), mat(C.eyeWhite, { roughness: 0.25 }), { sx: 0.17, sy: 0.2, sz: 0.1, shadow: false }));
    const look = new THREE.Group(); eye.add(look);   // iris, pupil and highlight move together when he looks around
    look.add(mesh(sphere(1), mat(C.iris, { roughness: 0.2 }), { sx: 0.11, sy: 0.13, sz: 0.05, z: 0.07, shadow: false }));
    look.add(mesh(sphere(1), mat(C.pupil, { roughness: 0.2 }), { sx: 0.06, sy: 0.075, sz: 0.03, z: 0.1, shadow: false }));
    look.add(mesh(sphere(1), mat(0xffffff, { emissive: 0xffffff, emissiveIntensity: 0.6 }), { sx: 0.03, sy: 0.03, sz: 0.02, x: 0.035, y: 0.05, z: 0.12, shadow: false }));
    const brow = mesh(capsule(0.03, 0.18), mat(C.brow), { x: side * 0.27, y: 0.33, z: 0.66, rz: Math.PI / 2 - side * 0.08, shadow: false });
    head.add(brow);
    eyes.push({ eye, look, brow, side });
  }

  // glasses: thin dark round frames
  // (in their own group, so they can go askew when he is startled)
  const frame = mat(0x23262b, { roughness: 0.35, metalness: 0.3 });
  const glasses = new THREE.Group(); glasses.position.set(0, 0.07, 0.75); head.add(glasses);
  for (const side of [-1, 1]) {
    glasses.add(mesh(new THREE.TorusGeometry(0.225, 0.02, 10, 40), frame, { x: side * 0.27, y: -0.01, z: -0.02, ry: side * 0.22, shadow: false }));
    glasses.add(mesh(new THREE.CylinderGeometry(0.016, 0.016, 0.7, 8), frame, { x: side * 0.63, y: 0.01, z: -0.37, rx: Math.PI / 2, shadow: false }));
  }
  glasses.add(mesh(new THREE.CylinderGeometry(0.016, 0.016, 0.12, 8), frame, { y: 0.01, z: 0.04, rz: Math.PI / 2, shadow: false }));

  // mouth: a smile that can open while talking
  const mouth = new THREE.Group(); mouth.position.set(0.04, -0.3, 0.68); head.add(mouth);
  const smile = mesh(new THREE.TorusGeometry(0.1, 0.022, 8, 24, Math.PI), mat(C.mouth), { rz: Math.PI, rx: -0.2, shadow: false });
  mouth.add(smile);
  const open = mesh(sphere(1), mat(C.mouth, { roughness: 0.4 }), { sx: 0.09, sy: 0.07, sz: 0.04, y: -0.05, shadow: false });
  open.add(mesh(sphere(1), mat(C.tongue), { sx: 0.6, sy: 0.4, sz: 0.5, y: -0.4, z: 0.3, shadow: false }));
  open.visible = false; mouth.add(open);

  // hair: tousled chestnut, swept to one side, covering the tops of the ears
  const hairMat = mat(C.hair, { roughness: 0.7 }), hairDarkMat = mat(C.hairDark, { roughness: 0.75 });
  head.add(mesh(sphere(0.8, 48), hairMat, { sx: 1.04, sy: 0.82, sz: 0.98, y: 0.2, z: -0.05 }));
  const tufts = [
    [-0.45, 0.62, 0.32, 0.26, -0.5], [-0.15, 0.72, 0.42, 0.28, -0.25], [0.18, 0.7, 0.42, 0.26, 0.1], [0.45, 0.6, 0.3, 0.24, 0.4],
    [-0.62, 0.35, 0.3, 0.22, -0.9], [0.66, 0.32, 0.26, 0.22, 0.9], [0.0, 0.82, 0.0, 0.3, 0], [-0.3, 0.75, -0.3, 0.28, -0.3], [0.3, 0.74, -0.32, 0.28, 0.3],
  ];
  for (const [x, y, z, r, rz] of tufts) head.add(mesh(sphere(1), hairMat, { sx: r, sy: r * 0.8, sz: r, x, y, z, rz }));
  // the fringe, swept to his right with loose locks over the forehead
  for (const [x, y, z, rz, s] of [[-0.32, 0.5, 0.58, 0.9, 1], [-0.08, 0.52, 0.64, 0.7, 1.1], [0.16, 0.5, 0.62, 0.5, 0.9], [0.36, 0.44, 0.55, 0.3, 0.8]]) {
    head.add(mesh(new THREE.ConeGeometry(0.11 * s, 0.42 * s, 16), hairDarkMat, { x, y, z, rz: Math.PI / 2 + rz, rx: 0.5 }));
  }

  // headset with a fold-down visor
  const hsMat = mat(C.headset, { roughness: 0.35, metalness: 0.2 });
  head.add(mesh(new THREE.TorusGeometry(0.86, 0.045, 12, 48, Math.PI), hsMat, { y: 0.02, z: -0.05 }));
  for (const side of [-1, 1]) head.add(mesh(new THREE.CylinderGeometry(0.17, 0.17, 0.12, 28), hsMat, { x: side * 0.86, y: 0.02, z: -0.05, rz: Math.PI / 2 }));
  const visorPivot = new THREE.Group(); visorPivot.position.set(0, 0.02, -0.05); head.add(visorPivot);
  const visorMat = new THREE.MeshStandardMaterial({ color: 0x9ff5a6, emissive: C.visorGlow, emissiveIntensity: 0.0, transparent: true, opacity: 0.35, roughness: 0.1, metalness: 0.1, side: THREE.DoubleSide });
  const visor = mesh(new THREE.CylinderGeometry(0.9, 0.9, 0.22, 48, 1, true, -1.15, 2.3), visorMat, { y: 0.06, shadow: false });
  visorPivot.add(visor);
  const dots = [];
  for (let i = -1; i <= 1; i++) {
    const d = mesh(sphere(1, 12), new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0 }), { sx: 0.03, sy: 0.03, sz: 0.01, x: i * 0.16, y: 0.06, z: 0.9, shadow: false });
    visorPivot.add(d); dots.push(d);
  }

  return { root, body, torso, head, neck, arms, legs, eyes, glasses, mouth, smile, open, visorPivot, visorMat, dots, laptop, holders, props, water, steam };
}

// lite: for the pet window, which renders in software (no GPU): no real-time shadows, lower pixel density,
// and at most `fps` frames a second.
export function createBen(canvas, { orbit = false, zoom = 1, lite = false, fps = 60 } = {}) {
  detail = lite ? 0.45 : 1;
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: !lite, alpha: true, powerPreference: 'low-power' });
  renderer.setPixelRatio(Math.min(lite ? 1.5 : 2, window.devicePixelRatio || 1));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = !lite;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(28, 1, 0.1, 100);
  camera.position.set(0, 2.2, 10.5);
  camera.lookAt(0, 1.85, 0);
  camera.zoom = zoom; camera.updateProjectionMatrix();

  // soft studio lighting: sky/ground fill, a warm key with soft shadows, a cool rim from behind
  scene.add(new THREE.HemisphereLight(0xffffff, 0xc9b9a6, 1.1));
  const key = new THREE.DirectionalLight(0xfff2e2, 2.2);
  key.position.set(3, 6, 5); key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024); key.shadow.radius = 6;
  Object.assign(key.shadow.camera, { left: -3, right: 3, top: 5, bottom: -1, near: 1, far: 20 });
  scene.add(key);
  const rim = new THREE.DirectionalLight(0xcfe3ff, 1.4); rim.position.set(-4, 4, -5); scene.add(rim);
  const front = new THREE.DirectionalLight(0xffffff, 0.5); front.position.set(0, 2, 8); scene.add(front);

  const ground = new THREE.Mesh(new THREE.PlaneGeometry(2.6, 2.6), new THREE.MeshBasicMaterial({ map: softShadowTexture(), transparent: true, depthWrite: false }));
  ground.rotation.x = -Math.PI / 2; ground.position.y = 0.0; scene.add(ground);

  const ben = buildBen();
  scene.add(ben.root);

  let state = 'idle', since = performance.now(), nextBlink = 1500, talkLevel = 0, dragYaw = 0, dragging = false, lastX = 0;
  let night = false, facing = 1, speaking = false, lookX = 0, lookY = 0, eyeX = 0, eyeY = 0, rTilt = 0, glassesZ = 0, waterLevel = 1;
  const propScale = {};
  const qBody = new THREE.Quaternion(), qParent = new THREE.Quaternion(), qTilt = new THREE.Quaternion(), eTilt = new THREE.Euler();
  // States where his eyes (and a little of his head) follow the mouse pointer.
  const NIGHT_MUG = new Set(['idle', 'talk', 'wave', 'quiet', 'sit', 'look']);
  const LOOKERS = new Set(['idle', 'sleepy', 'listen', 'sit', 'water', 'watch', 'happy', 'quiet', 'talk']);
  if (orbit) {
    canvas.addEventListener('pointerdown', (e) => { dragging = true; lastX = e.clientX; });
    addEventListener('pointerup', () => { dragging = false; });
    addEventListener('pointermove', (e) => { if (dragging) { dragYaw += (e.clientX - lastX) * 0.01; lastX = e.clientX; } });
  }

  function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== Math.round(w * renderer.getPixelRatio()) || canvas.height !== Math.round(h * renderer.getPixelRatio())) {
      renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
    }
  }

  const lerp = (a, b, t) => a + (b - a) * t;
  function pose(dt, t) {
    if (state === 'manual') return;              // for tuning poses by hand
    const k = 1 - Math.pow(0.001, dt);           // smoothing that is frame-rate independent
    const s = (t - since) / 1000;
    const L = ben.arms.left, R = ben.arms.right;
    const target = {
      bodyY: 0, bodyRotZ: 0, headX: 0, headZ: 0, headY: 0,
      lUpperX: 0, lUpperZ: -0.12, lElbowX: 0, lElbowZ: 0, rUpperX: 0, rUpperZ: 0.12, rElbowX: 0, rElbowZ: 0,
      visorDown: 0, glow: 0, eyesOpen: 1, browY: 0, lLegX: 0, rLegX: 0, lLegZ: 0, rLegZ: 0,
    };
    const breathe = Math.sin(t / 700) * 0.015;
    switch (state) {
      case 'idle':
        target.headZ = Math.sin(t / 1900) * 0.05; target.headY = Math.sin(t / 2600) * 0.12;
        break;
      case 'wave':
        target.bodyY = Math.max(0, Math.sin(Math.min(s, 0.5) * Math.PI * 2)) * 0.15;
        target.rUpperZ = 2.5; target.rUpperX = -0.2; target.rElbowX = 0; target.rElbowZ = 0.4 + Math.sin(t / 110) * 0.5;
        target.headZ = -0.1; target.browY = 0.03;
        break;
      case 'listen':
        target.visorDown = 1; target.glow = 1.1 + Math.sin(t / 220) * 0.25;
        target.lUpperZ = -2.4; target.lElbowZ = -1.2;                            // hand cupped by his ear
        target.headZ = 0.1; target.headY = -0.1;
        break;
      case 'think':
        // coding on his laptop: both hands typing, eyes on the screen
        target.laptop = 1;
        target.lUpperX = -0.75 + Math.sin(t / 90) * 0.04; target.lUpperZ = 0.1; target.lElbowX = -0.85;
        target.rUpperX = -0.75 + Math.sin(t / 90 + 2) * 0.04; target.rUpperZ = -0.1; target.rElbowX = -0.85;
        target.headX = 0.22; target.headY = Math.sin(t / 900) * 0.08; target.browY = -0.02;
        break;
      case 'talk':
        target.lUpperZ = -0.5 - Math.sin(t / 380) * 0.2; target.lElbowX = -0.9 - Math.sin(t / 300) * 0.3;
        target.rUpperZ = 0.35 + Math.sin(t / 420 + 1) * 0.15; target.rElbowX = -0.4;
        target.headZ = Math.sin(t / 600) * 0.06; target.headX = Math.sin(t / 330) * 0.04;
        break;
      case 'quiet':
        target.visorDown = 1; target.glow = 0.9;
        target.rUpperX = -1.75; target.rUpperY = -0.6; target.rUpperZ = -0.55; target.rElbowX = -2.05;  // hand up to his mouth: "shh"
        break;
      case 'held':
        // picked up: arms flung up, legs kicking, eyes wide
        target.lUpperZ = -2.7; target.rUpperZ = 2.7; target.lElbowZ = -0.3; target.rElbowZ = 0.3;
        target.lLegX = Math.sin(t / 120) * 0.55; target.rLegX = -Math.sin(t / 120) * 0.55;
        target.headX = -0.15; target.headZ = Math.sin(t / 260) * 0.1; target.browY = 0.06; target.eyesOpen = 1.25;
        break;
      case 'fly':
        // thrown: arms and legs spread like a starfish
        target.lUpperZ = -1.9; target.rUpperZ = 1.9; target.lLegZ = -0.45; target.rLegZ = 0.45;
        target.headX = -0.1; target.browY = 0.06; target.eyesOpen = 1.2;
        break;
      case 'dizzy':
        // a hard landing: head going round in circles, arms hanging loose
        target.headZ = Math.sin(t / 160) * 0.22; target.headX = Math.cos(t / 160) * 0.12;
        target.bodyRotZ = Math.sin(t / 320) * 0.06; target.eyesOpen = 0.35; target.browY = -0.03;
        target.lUpperZ = -0.35; target.rUpperZ = 0.35;
        break;
      case 'sleep':
        target.eyesOpen = 0.08; target.headX = 0.32; target.headZ = 0.1; target.bodyY = -0.02;
        target.lUpperZ = -0.25; target.rUpperZ = 0.25;
        break;

      // ---- coming and going
      case 'look':     // just come up: a quick look round
        target.headY = Math.sin(s * 4.2) * 0.55; target.browY = 0.03;
        break;
      case 'climb': {  // hand over hand on a rope that hangs in front of him
        const w = t / 150;
        target.lUpperX = -1.5 + Math.sin(w) * 0.3; target.lUpperZ = 0.45; target.lElbowX = -0.4 - Math.max(0, Math.sin(w)) * 0.7;
        target.rUpperX = -1.5 - Math.sin(w) * 0.3; target.rUpperZ = -0.45; target.rElbowX = -0.4 - Math.max(0, -Math.sin(w)) * 0.7;
        target.lLegX = -0.4; target.rLegX = -0.3; target.lLegZ = 0.12; target.rLegZ = -0.12;   // knees together round the rope
        target.headX = 0.1; target.browY = 0.03;
        break;
      }
      case 'float':    // drifting down under an umbrella (the page draws it), looking where he will land
        target.rUpperZ = 2.85; target.rElbowZ = 0.25; target.rUpperX = -0.1;
        target.lUpperZ = -0.5 + Math.sin(t / 700) * 0.1;
        target.lLegX = Math.sin(t / 600) * 0.25; target.rLegX = -Math.sin(t / 600) * 0.25;
        target.bodyRotZ = Math.sin(t / 900) * 0.05; target.headX = 0.15; target.eyesOpen = 0.85;
        break;
      case 'peek':     // leaning in round the edge of the screen, with a little wave
        target.bodyRotZ = 0.25; target.headZ = 0.2; target.headY = -0.25; target.browY = 0.05; target.eyesOpen = 1.15;
        target.lUpperZ = -2.3; target.lElbowZ = -0.6 + Math.sin(t / 120) * 0.35;
        break;
      case 'walk': case 'run': {
        const fast = state === 'run', w = t / (fast ? 80 : 150), a = fast ? 0.8 : 0.5;
        target.lLegX = Math.sin(w) * a; target.rLegX = -Math.sin(w) * a;
        target.lUpperX = -Math.sin(w) * a * 0.9; target.rUpperX = Math.sin(w) * a * 0.9;
        if (fast) { target.lElbowX = -1.1; target.rElbowX = -1.1; target.headX = 0.1; }
        target.bodyY = Math.abs(Math.sin(w)) * (fast ? 0.12 : 0.05);
        break;
      }

      // ---- reminders
      case 'water':    // holding out a glass of water, nodding: "drink up"
        target.glass = 1;
        target.rUpperX = -1.05; target.rUpperZ = -0.15; target.rElbowX = -0.55;
        target.lUpperZ = -0.2; target.lUpperX = -0.25; target.lElbowX = -0.5;   // other hand relaxed
        target.headX = Math.max(0, Math.sin(t / 260)) * 0.14 - 0.04; target.headZ = 0.08; target.browY = 0.03;
        break;
      case 'sip':      // he takes a sip too, then "ahh"
        target.glass = 1;
        if (s < 1.8) {
          target.rUpperX = -1.6; target.rUpperY = -0.5; target.rUpperZ = -0.45; target.rElbowX = -1.8;
          target.rTilt = 0.9 * Math.min(1, s / 0.6); target.headX = -0.22 * Math.min(1, s / 0.6); target.eyesOpen = 0.25;
        } else {
          target.rUpperX = -1.0; target.rUpperZ = -0.1; target.rElbowX = -0.5;
          target.mouthOpen = 0.6; target.browY = 0.04; target.eyesOpen = 0.2;
        }
        if (s > 1.0) target.waterLevel = 0.55;
        break;
      case 'stretch': {  // break time: arms up, leaning side to side, the odd yawn
        const lean = Math.sin(t / 1100), c = s % 6, yawn = c < 1.6 ? Math.sin(c / 1.6 * Math.PI) : 0;
        target.lUpperZ = -2.75 - lean * 0.1; target.rUpperZ = 2.75 - lean * 0.1; target.lElbowZ = -0.7; target.rElbowZ = 0.7;
        target.bodyRotZ = lean * 0.13; target.headZ = lean * 0.1;
        target.mouthOpen = yawn; target.eyesOpen = 0.45 - yawn * 0.3; target.headX = -yawn * 0.2;
        break;
      }
      case 'watch': {  // taps his watch, then points at you: "it's time"
        target.watch = 1;
        target.lUpperX = -0.6; target.lUpperZ = 0.35; target.lElbowX = -1.9;   // forearm up in front of him, watch showing
        if (s % 3.2 < 2) {
          target.rUpperX = -0.9; target.rUpperZ = -0.5; target.rElbowX = -1.3 + Math.max(0, Math.sin(t / 70)) * 0.25;
          target.headX = 0.2; target.headY = -0.2;
        } else {
          target.rUpperX = -1.5; target.rUpperZ = 0.3; target.rElbowX = -0.05; target.browY = 0.05;   // and points at you
        }
        break;
      }
      case 'clipboard': {  // ticking things off a checklist, looking up at you now and then
        target.clipboard = 1; target.pencil = 1;
        target.lUpperX = -0.95; target.lUpperZ = 0.2; target.lElbowX = -1.0;
        target.rUpperX = -0.95; target.rUpperZ = -0.35; target.rElbowX = -0.9 + (s % 1.4 < 0.5 ? Math.sin(t / 55) * 0.15 : 0);
        if (s % 4 > 2.6) { target.browY = 0.04; } else { target.headX = 0.18; target.headY = -0.12; }
        break;
      }
      case 'happy':    // thumbs up, with a hop
        target.thumb = 1;
        target.rUpperX = -0.55; target.rUpperZ = 0.45; target.rElbowX = -1.5;
        target.lUpperZ = -0.3;
        target.bodyY = s < 1.2 ? Math.abs(Math.sin(s * Math.PI * 2.5)) * 0.35 : 0;
        target.eyesOpen = 0.35; target.browY = 0.04; target.headZ = -0.08;
        break;

      // ---- in between
      case 'startle': {  // poked: a jump, arms out, glasses knocked askew
        const p = Math.max(0, Math.sin(Math.min(1, s / 0.55) * Math.PI));
        target.bodyY = p * 0.38; target.lUpperZ = -1.6; target.rUpperZ = 1.6; target.lElbowZ = -0.6; target.rElbowZ = 0.6;
        target.lLegX = p * 0.3; target.rLegX = -p * 0.3;
        target.eyesOpen = 1.35; target.browY = 0.08; target.mouthOpen = 0.5; target.glassesZ = 0.3;
        break;
      }
      case 'sit':      // sitting on the taskbar, swinging his feet
        target.bodyY = -0.82; target.lLegX = -1.45 + Math.sin(t / 380) * 0.18; target.rLegX = -1.45 - Math.sin(t / 380) * 0.18;
        target.lUpperZ = -0.55; target.rUpperZ = 0.55; target.lElbowX = -0.3; target.rElbowX = -0.3;
        target.headZ = Math.sin(t / 1700) * 0.06; target.headY = Math.sin(t / 2300) * 0.15;
        break;
      case 'yawn': {   // a big yawn and stretch (about two seconds)
        const p = Math.sin(Math.min(1, s / 2) * Math.PI);
        target.lUpperZ = -0.12 - p * 2.6; target.rUpperZ = 0.12 + p * 2.6; target.lElbowZ = -p * 0.6; target.rElbowZ = p * 0.6;
        target.mouthOpen = p; target.eyesOpen = 1 - p * 0.85; target.headX = -p * 0.25; target.bodyY = p * 0.04;
        break;
      }
      case 'sleepy': {   // late in the day: slower, heavy eyes, a mug of something warm, a yawn now and then
        target.mug = 1;
        const c = s % 10;
        target.lUpperX = -0.85; target.lUpperZ = 0.15; target.lElbowX = -1.15;
        if (c > 3 && c < 4.6) { target.lUpperX = -1.45; target.lElbowX = -1.75; target.headX = -0.1; target.eyesOpen = 0.2; }   // a sip
        else { target.eyesOpen = 0.55; target.headX = 0.08; }
        target.headZ = Math.sin(t / 2600) * 0.08;
        if (c > 7 && c < 9) {
          const y = Math.sin((c - 7) / 2 * Math.PI);
          target.mouthOpen = y; target.eyesOpen = 0.55 - y * 0.45;
          target.rUpperX = -y * 1.5; target.rUpperZ = 0.12 - y * 0.4; target.rElbowX = -y * 1.9;   // hand over his mouth
        }
        break;
      }
    }
    // Late in the day he keeps his mug in his left hand and his eyes get heavy, whatever he is doing with the other hand.
    if (night && NIGHT_MUG.has(state)) {
      target.mug = 1; target.lUpperX = -0.85; target.lUpperZ = 0.15; target.lElbowX = -1.15; target.lElbowZ = 0;
      target.eyesOpen = Math.min(target.eyesOpen, 0.62);
    }
    if (LOOKERS.has(state)) { target.headY += lookX * 0.4; target.headX += lookY * 0.2; }

    ben.body.position.y = lerp(ben.body.position.y, target.bodyY, k);
    ben.body.rotation.z = lerp(ben.body.rotation.z, target.bodyRotZ, k);
    ben.legs[0].rotation.x = lerp(ben.legs[0].rotation.x, target.lLegX, k);
    ben.legs[1].rotation.x = lerp(ben.legs[1].rotation.x, target.rLegX, k);
    ben.legs[0].rotation.z = lerp(ben.legs[0].rotation.z, target.lLegZ, k);
    ben.legs[1].rotation.z = lerp(ben.legs[1].rotation.z, target.rLegZ, k);
    ben.torso.scale.set(1 + breathe * 0.5, 1 + breathe, 1 + breathe * 0.5);
    ben.head.rotation.x = lerp(ben.head.rotation.x, target.headX + breathe, k);
    ben.head.rotation.z = lerp(ben.head.rotation.z, target.headZ, k);
    ben.head.rotation.y = lerp(ben.head.rotation.y, target.headY, k);
    L.upper.rotation.x = lerp(L.upper.rotation.x, target.lUpperX, k);
    L.upper.rotation.z = lerp(L.upper.rotation.z, target.lUpperZ, k);
    L.elbow.rotation.x = lerp(L.elbow.rotation.x, target.lElbowX, k);
    L.elbow.rotation.z = lerp(L.elbow.rotation.z, target.lElbowZ, k);
    R.upper.rotation.x = lerp(R.upper.rotation.x, target.rUpperX, k);
    R.upper.rotation.y = lerp(R.upper.rotation.y, target.rUpperY || 0, k);
    R.upper.rotation.z = lerp(R.upper.rotation.z, target.rUpperZ, k);
    R.elbow.rotation.x = lerp(R.elbow.rotation.x, target.rElbowX, k);
    R.elbow.rotation.z = lerp(R.elbow.rotation.z, target.rElbowZ, k);

    // props: grow in when the pose uses them, shrink away when it does not; the holders keep them level
    for (const [name, p] of Object.entries(ben.props)) {
      const v = lerp(propScale[name] || 0.001, target[name] ? 1 : 0.001, Math.min(1, k * 1.5));
      propScale[name] = v; p.scale.setScalar(v); p.visible = v > 0.03;
    }
    rTilt = lerp(rTilt, target.rTilt || 0, k);
    ben.root.updateMatrixWorld(true);
    ben.body.getWorldQuaternion(qBody);
    for (const side of ['left', 'right']) {
      const h = ben.holders[side];
      h.parent.getWorldQuaternion(qParent);
      h.quaternion.copy(qParent.invert().multiply(qBody));
      if (side === 'right' && rTilt > 0.01) h.quaternion.multiply(qTilt.setFromEuler(eTilt.set(0, 0, rTilt)));
    }
    waterLevel = lerp(waterLevel, target.waterLevel || (state === 'sip' ? waterLevel : 1), k * 0.5);
    ben.water.scale.y = waterLevel; ben.water.position.y = -0.21 + 0.15 * waterLevel;
    if (ben.props.mug.visible) ben.steam.forEach((puff, i) => {
      const c = ((t / 1600) + i * 0.5) % 1;
      puff.position.set(Math.sin(c * 6 + i) * 0.04, 0.2 + c * 0.35, 0); puff.material.opacity = Math.sin(c * Math.PI) * 0.5;
    });
    glassesZ = lerp(glassesZ, target.glassesZ || 0, target.glassesZ ? k * 2 : k * 0.25);   // knocked askew fast, straightened slowly
    ben.glasses.rotation.z = glassesZ;

    // visor: folds down over the eyes and glows green (amber when he has to stay quiet)
    ben.visorPivot.rotation.x = lerp(ben.visorPivot.rotation.x, target.visorDown ? 0 : -1.25, k);
    ben.visorMat.emissive.setHex(state === 'quiet' ? C.visorQuiet : C.visorGlow);
    ben.visorMat.color.setHex(state === 'quiet' ? 0xffd27a : 0x9ff5a6);
    ben.visorMat.emissiveIntensity = lerp(ben.visorMat.emissiveIntensity, target.glow, k);
    ben.visorMat.opacity = lerp(ben.visorMat.opacity, target.visorDown ? 0.55 : 0.3, k);
    ben.dots.forEach((d) => { d.material.opacity = 0; });
    const ls = lerp(ben.laptop.scale.x, target.laptop ? 1 : 0.001, k);
    ben.laptop.scale.setScalar(ls); ben.laptop.visible = ls > 0.03;

    // blinking, and the mouth moving while he talks
    nextBlink -= dt * 1000;
    let open = target.eyesOpen;
    if (nextBlink < 0) { if (nextBlink < -130) nextBlink = 2200 + Math.random() * 3000; else open = Math.min(open, 0.1); }
    const looking = LOOKERS.has(state);
    eyeX = lerp(eyeX, looking ? lookX : 0, k); eyeY = lerp(eyeY, looking ? lookY : 0, k);
    for (const e of ben.eyes) {
      e.eye.scale.y = lerp(e.eye.scale.y, open, Math.min(1, k * 3));
      e.brow.position.y = lerp(e.brow.position.y, 0.33 + target.browY, k);
      e.look.position.set(eyeX * 0.04, -eyeY * 0.035, 0);
    }
    // the mouth moves while he talks (also while holding up a prop), and opens for a yawn or an "ahh"
    const talking = state === 'talk' || (speaking && !['sip', 'yawn', 'sleep'].includes(state));
    const level = Math.max(talking ? (talkLevel || (0.5 + 0.5 * Math.sin(t / 90) * Math.sin(t / 210))) : 0, target.mouthOpen || 0);
    ben.open.visible = level > 0.15;
    ben.smile.visible = !ben.open.visible;
    ben.open.scale.y = 0.04 + level * 0.06;

    let yaw = dragYaw;
    if (state === 'idle') yaw += Math.sin(t / 3000) * 0.15;
    else if (state === 'walk' || state === 'run') yaw += facing * 0.9;   // turned the way he is going
    else if (state === 'peek') yaw -= 0.35;
    ben.root.rotation.y = lerp(ben.root.rotation.y, yaw, k);
  }

  let last = performance.now(), raf = 0, running = true;
  let minGap = 1000 / fps - 2;
  function frame(t) {
    raf = requestAnimationFrame(frame);
    if (t - last < minGap) return;
    const dt = Math.min(0.1, (t - last) / 1000); last = t;
    resize(); pose(dt, t);
    renderer.render(scene, camera);
  }
  raf = requestAnimationFrame(frame);

  return {
    setState(next) { if (next !== state) { state = next; since = performance.now(); } },
    setFps(n) { minGap = 1000 / n - 2; },
    // In the air: the soft shadow under his feet goes (the page draws one on the floor instead).
    setAirborne(on) { ground.visible = !on; },
    setTalkLevel(v) { talkLevel = Math.max(0, Math.min(1, v)); },
    // His mouth moves with the voice even in poses other than 'talk' (holding the glass of water, say).
    setSpeaking(on) { speaking = !!on; },
    // Which way he faces while walking: -1 left, 1 right.
    setFacing(dir) { facing = dir < 0 ? -1 : 1; },
    // Night: the mug and sleepy eyes in most poses (the 'sleepy' idle has them anyway).
    setNight(on) { night = !!on; },
    // Where the mouse pointer is, from -1 to 1 each way (0, 0: straight ahead). His eyes and head follow it.
    setLook(x, y) { lookX = Math.max(-1, Math.min(1, x)); lookY = Math.max(-1, Math.min(1, y)); },
    pause() { if (running) { cancelAnimationFrame(raf); running = false; } },
    resume() { if (!running) { running = true; last = performance.now(); raf = requestAnimationFrame(frame); } },
    get state() { return state; },
    parts: ben,
    get info() { return renderer.info.render; },
    // Draw one frame now, advancing the animation by `ms` (works even when the page is hidden).
    renderNow(ms = 16) { const t = performance.now(); resize(); pose(ms / 1000, t); renderer.render(scene, camera); },
  };
}
