/**
 * field.js — SSL field Canvas renderer
 *
 * SSL coordinate system: origin at center, x=right, y=up
 * Standard Div B field: 9m × 6m (half: 4.5m × 3.0m)
 */

const FIELD = {
  halfLength: 4.5,   // meters
  halfWidth: 3.0,    // meters
  goalWidth: 1.0,    // meters (half = 0.5)
  goalDepth: 0.18,   // meters
  penaltyDepth: 1.0, // meters
  penaltyWidth: 2.0, // meters (half = 1.0)
  centerRadius: 0.5, // meters
  robotRadius: 0.09, // meters
  ballRadius: 0.043, // meters
};

function positiveNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : null;
}

const TEAM_COLOR = {
  blue:   '#388bfd',
  yellow: '#e3b341',
};

class FieldRenderer {
  constructor(canvas) {
    this._canvas = canvas;
    this._ctx = canvas.getContext('2d');
    this._padding = 24; // px
    this._field = { ...FIELD };
    this._scale = 1;
    this._ox = 0; // canvas origin x (center of field)
    this._oy = 0; // canvas origin y (center of field)
    this._resize();
    this._resizeObserver = new ResizeObserver(() => this._resize());
    this._resizeObserver.observe(canvas);
  }

  _resize(force = false) {
    const rect = this._canvas.parentElement.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width));
    const h = Math.max(1, Math.round(rect.height));
    const sizeChanged = w !== this._canvas.width || h !== this._canvas.height;
    if (!sizeChanged && !force) return;
    if (sizeChanged) {
      this._canvas.width = w;
      this._canvas.height = h;
    }
    const scaleX = Math.max(1, w - this._padding * 2) / (this._field.halfLength * 2);
    const scaleY = Math.max(1, h - this._padding * 2) / (this._field.halfWidth * 2);
    this._scale = Math.min(scaleX, scaleY);
    this._ox = w / 2;
    this._oy = h / 2;
  }

  _setFieldGeometry(geometry) {
    const length = positiveNumber(geometry?.length);
    const width = positiveNumber(geometry?.width);
    const next = {
      ...FIELD,
      halfLength: length ? length / 2 : FIELD.halfLength,
      halfWidth: width ? width / 2 : FIELD.halfWidth,
      goalWidth: positiveNumber(geometry?.goal_width) ?? FIELD.goalWidth,
      goalDepth: positiveNumber(geometry?.goal_depth) ?? FIELD.goalDepth,
      penaltyDepth: positiveNumber(geometry?.penalty_depth) ?? FIELD.penaltyDepth,
      penaltyWidth: positiveNumber(geometry?.penalty_width) ?? FIELD.penaltyWidth,
    };
    const changed = Object.keys(next).some(key => next[key] !== this._field[key]);
    if (changed) {
      this._field = next;
      this._resize(true);
    }
  }

  /** Convert SSL coordinates (meters) to canvas pixels */
  _px(x, y) {
    return [this._ox + x * this._scale, this._oy - y * this._scale];
  }

  /** Convert meters to pixels */
  _m(m) { return m * this._scale; }

  draw(fieldSnapshot, gameState) {
    this._setFieldGeometry(fieldSnapshot?.field);
    this._resize();

    const ctx = this._ctx;
    ctx.clearRect(0, 0, this._canvas.width, this._canvas.height);

    this._drawField(ctx);
    this._drawLines(ctx);

    if (fieldSnapshot) {
      // Draw trajectory first (under robots/ball)
      if (fieldSnapshot.ball_trail) {
        this._drawBallTrail(ctx, fieldSnapshot.ball_trail);
      }
      this._drawRobots(ctx, fieldSnapshot.robots_blue, fieldSnapshot.robots_yellow);
      this._drawBall(ctx, fieldSnapshot.ball);
    }
  }

  _drawField(ctx) {
    const field = this._field;
    const [fx, fy] = this._px(-field.halfLength, field.halfWidth);
    const fw = this._m(field.halfLength * 2);
    const fh = this._m(field.halfWidth * 2);
    ctx.fillStyle = '#1a4a1e';
    ctx.fillRect(fx, fy, fw, fh);

    // Subtle stripe pattern
    ctx.fillStyle = 'rgba(255,255,255,0.025)';
    const stripeW = this._m(0.5);
    for (let i = 0; i < fw / stripeW; i += 2) {
      ctx.fillRect(fx + i * stripeW, fy, stripeW, fh);
    }
  }

  _drawLines(ctx) {
    const field = this._field;
    ctx.strokeStyle = 'rgba(255,255,255,0.7)';
    ctx.lineWidth = Math.max(1, this._m(0.02));

    // Field boundary
    this._rect(ctx, -field.halfLength, -field.halfWidth,
      field.halfLength * 2, field.halfWidth * 2);

    // Center line
    const [cx0, cy0] = this._px(0, field.halfWidth);
    const [cx1, cy1] = this._px(0, -field.halfWidth);
    ctx.beginPath();
    ctx.moveTo(cx0, cy0);
    ctx.lineTo(cx1, cy1);
    ctx.stroke();

    // Center circle
    this._circle(ctx, 0, 0, field.centerRadius);

    // Penalty areas
    const pd = field.penaltyDepth;
    const pw = field.penaltyWidth;
    this._rect(ctx, -field.halfLength, -pw / 2, pd, pw);
    this._rect(ctx, field.halfLength - pd, -pw / 2, pd, pw);

    // Goals (filled)
    ctx.fillStyle = 'rgba(100,100,200,0.3)';
    this._fillRect(ctx, -field.halfLength - field.goalDepth, -field.goalWidth / 2,
      field.goalDepth, field.goalWidth);
    ctx.fillStyle = 'rgba(200,200,100,0.3)';
    this._fillRect(ctx, field.halfLength, -field.goalWidth / 2,
      field.goalDepth, field.goalWidth);

    // Goal lines
    ctx.strokeStyle = 'rgba(255,255,255,0.7)';
    this._rect(ctx, -field.halfLength - field.goalDepth, -field.goalWidth / 2,
      field.goalDepth, field.goalWidth);
    this._rect(ctx, field.halfLength, -field.goalWidth / 2,
      field.goalDepth, field.goalWidth);

    // Center dot
    ctx.fillStyle = 'rgba(255,255,255,0.7)';
    const [ccx, ccy] = this._px(0, 0);
    ctx.beginPath();
    ctx.arc(ccx, ccy, Math.max(2, this._m(0.03)), 0, Math.PI * 2);
    ctx.fill();
  }

  _drawRobots(ctx, blueRobots, yellowRobots) {
    const r = Math.max(4, this._m(this._field.robotRadius));
    if (blueRobots) {
      for (const robot of blueRobots) {
        this._drawRobot(ctx, robot, TEAM_COLOR.blue, r);
      }
    }
    if (yellowRobots) {
      for (const robot of yellowRobots) {
        this._drawRobot(ctx, robot, TEAM_COLOR.yellow, r);
      }
    }
  }

  _drawRobot(ctx, robot, color, r) {
    const [px, py] = this._px(robot.x, robot.y);

    // Shadow
    ctx.fillStyle = 'rgba(0,0,0,0.4)';
    ctx.beginPath();
    ctx.ellipse(px + 1, py + 1, r, r * 0.7, 0, 0, Math.PI * 2);
    ctx.fill();

    // Body
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(px, py, r, 0, Math.PI * 2);
    ctx.fill();

    // Orientation indicator (dribbler side)
    if (robot.theta !== undefined) {
      ctx.strokeStyle = 'rgba(0,0,0,0.8)';
      ctx.lineWidth = Math.max(1.5, r * 0.3);
      const dx = Math.cos(robot.theta) * r * 0.9;
      const dy = -Math.sin(robot.theta) * r * 0.9; // canvas y is flipped
      ctx.beginPath();
      ctx.moveTo(px, py);
      ctx.lineTo(px + dx, py + dy);
      ctx.stroke();
    }

    // Ball contact highlight
    if (robot.has_ball) {
      ctx.strokeStyle = '#f85149';
      ctx.lineWidth = Math.max(1, r * 0.25);
      ctx.beginPath();
      ctx.arc(px, py, r + 1.5, 0, Math.PI * 2);
      ctx.stroke();
    }

    // Robot ID label
    ctx.fillStyle = '#fff';
    ctx.font = `bold ${Math.max(8, Math.floor(r * 0.9))}px monospace`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(robot.id, px, py);
  }

  _drawBall(ctx, ball) {
    if (!ball) return;
    const [px, py] = this._px(ball.x, ball.y);
    const r = Math.max(3, this._m(this._field.ballRadius));

    // Glow
    const grad = ctx.createRadialGradient(px, py, 0, px, py, r * 2.5);
    grad.addColorStop(0, 'rgba(255,120,0,0.5)');
    grad.addColorStop(1, 'rgba(255,120,0,0)');
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(px, py, r * 2.5, 0, Math.PI * 2);
    ctx.fill();

    // Ball
    ctx.fillStyle = '#ff8800';
    ctx.beginPath();
    ctx.arc(px, py, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = '#cc5500';
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  _drawBallTrail(ctx, trail) {
    if (!trail || trail.length < 2) return;
    ctx.strokeStyle = 'rgba(255,136,0,0.3)';
    ctx.lineWidth = Math.max(1, this._m(0.02));
    ctx.beginPath();
    for (let i = 0; i < trail.length; i++) {
      const [px, py] = this._px(trail[i].x, trail[i].y);
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    }
    ctx.stroke();
  }

  // ===== Drawing helpers =====

  _rect(ctx, x, y, w, h) {
    const [px, py] = this._px(x, y + h);
    ctx.beginPath();
    ctx.strokeRect(px, py, this._m(w), this._m(h));
  }

  _fillRect(ctx, x, y, w, h) {
    const [px, py] = this._px(x, y + h);
    ctx.fillRect(px, py, this._m(w), this._m(h));
  }

  _circle(ctx, cx, cy, r) {
    const [px, py] = this._px(cx, cy);
    ctx.beginPath();
    ctx.arc(px, py, this._m(r), 0, Math.PI * 2);
    ctx.stroke();
  }
}
