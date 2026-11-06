---
name: signals-and-systems-week-4-tutor
description: "5CCYB020 Week 4 tutor note (fixture, invented): Fourier series and convolution, for the eval's tutor cases."
module_code: 5CCYB020
module: Signals and Systems
week: 4
tags: [module, tutor, generated]
generated_by: denisse-tutor
updated: 2026-11-04
---

# Signals and Systems Week 4 — tutor note

## Learning outcomes

- Write the Fourier series of a periodic signal and name the harmonics.
- Explain the Gibbs phenomenon at a jump and why it does not shrink with more terms.
- Use Parseval's theorem to get signal power from the coefficients.
- Compute the convolution of two signals and state the convolution theorem.

## Key methods and equations

- **Fourier series**: $x(t)=\sum_k c_k e^{jk\omega_0 t}$ — represent a periodic signal as a sum of harmonics.
- **Fourier coefficients**: $c_k=\frac{1}{T}\int_T x(t)e^{-jk\omega_0 t}dt$ — find each harmonic's weight.
- **Parseval's theorem**: $\frac{1}{T}\int_T |x(t)|^2 dt=\sum_k |c_k|^2$ — power from the coefficients.
- **Convolution**: $y(t)=\int x(\tau)h(t-\tau)d\tau$ — the output of an LTI system from its impulse response.
- **Convolution theorem**: $x*h \leftrightarrow X(\omega)H(\omega)$ — convolution in time is multiplication in frequency.

## Worked examples

**1. Square wave to its fifth harmonic**
1. Write the coefficients for odd k.
2. Sum the first three odd harmonics and sketch the overshoot.

**2. Convolve a rectangular pulse with itself**
1. Flip and slide h; the overlap grows then shrinks.
2. The result is a triangle twice as wide.

## Check yourself

- [ ] Why does the Gibbs overshoot stay at about 9%?
- [ ] What does convolution in time become in the frequency domain?

## Topics

- Fourier series (assumes: complex exponentials)
- Convolution (assumes: integration, LTI systems)
