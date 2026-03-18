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

class FieldRenderer {
  constructor(canvas) {
    this._canvas = canvas;
    this._ctx = canvas.getContext('2d');
    this._padding = 24; // px
    this._scale = 1;
    this._ox = 0; // canvas origin x (center of field)
    this._oy = 0; // canvas origin y (center of field)
    this._resize();
    this._resizeObserver = new ResizeObserver(() => this._resize());
    this._resizeObserver.observe(canvas);
  }

  _resize() {
    const rect = this._canvas.parentElement.getBoundingClientRect();
    this._canvas.width = rect.width;
    this._canvas.height = rect.height;
    const scaleX = (rect.width - this._padding * 2) / (FIELD.halfLength * 2);
    const scaleY = (rect.height - this._padding * 2) / (FIELD.halfWidth * 2);
    this._scale = Math.min(scaleX, scaleY);
    this._ox = rect.width / 2;
    this._oy = rect.height / 2;
  }

  /** Convert SSL coordinates (meters) to canvas pixels */
  _px(x, y) {
    return [this._ox + x * this._scale, this._oy - y * this._scale];
  }

  /** Convert meters to pixels */
  _m(m) { return m * this._scale; }

  draw(fieldSnapshot, gameState) {
    const ctx = this._ctx;
    ctx.clearRect(0, 0, this._canvas.width, this._canvas.height);

    this._drawField(ctx);
    this._drawLines(ctx);

    if (fieldSnapshot) {
      // Draw trajectory first (under robots/ball)
      if (fieldSnapshot.ball_trail) {
        this._drawBallTrail(ctx, fieldSnapshot.ball_trail);
      }
      this._drawRobots(ctx, fieldSnapshot.robots_ours, fieldSnapshot.robots_theirs);
      this._drawBall(ctx, fieldSnapshot.ball);
    }
  }

  _drawField(ctx) {
    const [fx, fy] = this._px(-FIELD.halfLength, FIELD.halfWidth);
    const fw = this._m(FIELD.halfLength * 2);
    const fh = this._m(FIELD.halfWidth * 2);
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
    ctx.strokeStyle = 'rgba(255,255,255,0.7)';
    ctx.lineWidth = Math.max(1, this._m(0.02));

    // Field boundary
    this._rect(ctx, -FIELD.halfLength, -FIELD.halfWidth,
      FIELD.halfLength * 2, FIELD.halfWidth * 2);

    // Center line
    const [cx0, cy0] = this._px(0, FIELD.halfWidth);
    const [cx1, cy1] = this._px(0, -FIELD.halfWidth);
    ctx.beginPath();
    ctx.moveTo(cx0, cy0);
    ctx.lineTo(cx1, cy1);
    ctx.stroke();

    // Center circle
    this._circle(ctx, 0, 0, FIELD.centerRadius);

    // Penalty areas
    const pd = FIELD.penaltyDepth;
    const pw = FIELD.penaltyWidth;
    this._rect(ctx, -FIELD.halfLength, -pw / 2, pd, pw);
    this._rect(ctx, FIELD.halfLength - pd, -pw / 2, pd, pw);

    // Goals (filled)
    ctx.fillStyle = 'rgba(100,100,200,0.3)';
    this._fillRect(ctx, -FIELD.halfLength - FIELD.goalDepth, -FIELD.goalWidth / 2,
      FIELD.goalDepth, FIELD.goalWidth);
    ctx.fillStyle = 'rgba(200,200,100,0.3)';
    this._fillRect(ctx, FIELD.halfLength, -FIELD.goalWidth / 2,
      FIELD.goalDepth, FIELD.goalWidth);

    // Goal lines
    ctx.strokeStyle = 'rgba(255,255,255,0.7)';
    this._rect(ctx, -FIELD.halfLength - FIELD.goalDepth, -FIELD.goalWidth / 2,
      FIELD.goalDepth, FIELD.goalWidth);
    this._rect(ctx, FIELD.halfLength, -FIELD.goalWidth / 2,
      FIELD.goalDepth, FIELD.goalWidth);

    // Center dot
    ctx.fillStyle = 'rgba(255,255,255,0.7)';
    const [ccx, ccy] = this._px(0, 0);
    ctx.beginPath();
    ctx.arc(ccx, ccy, Math.max(2, this._m(0.03)), 0, Math.PI * 2);
    ctx.fill();
  }

  _drawRobots(ctx, oursRobots, theirsRobots) {
    const r = Math.max(4, this._m(FIELD.robotRadius));
    if (oursRobots) {
      for (const robot of oursRobots) {
        this._drawRobot(ctx, robot, '#388bfd', r);
      }
    }
    if (theirsRobots) {
      for (const robot of theirsRobots) {
        this._drawRobot(ctx, robot, '#e3b341', r);
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
    const r = Math.max(3, this._m(FIELD.ballRadius));

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
