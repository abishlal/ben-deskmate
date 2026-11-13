// Puts the 3D Ben (ben3d.js) in the pet window. pet.js drives him through window.ben3d.
// If WebGL is not available, nothing changes and the drawn avatar stays.
import { createBen } from './ben3d.js';

const flip = document.getElementById('flip');
const canvas = document.createElement('canvas');
canvas.id = 'ben3d';
try {
  const probe = document.createElement('canvas').getContext('webgl2') || document.createElement('canvas').getContext('webgl');
  if (!probe) throw new Error('no WebGL');
  flip.prepend(canvas);
  window.ben3d = createBen(canvas, { zoom: 1.2, lite: true, fps: 24 });
  document.getElementById('pet').classList.add('three');
  document.body.classList.add('three');
} catch (err) {
  canvas.remove();
  console.warn('3D Ben unavailable, using the drawn avatar:', err && err.message);
}
