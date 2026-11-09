// meetrec : enregistre l'audio système et le micro dans deux fichiers WAV séparés.
// Usage : meetrec <dossier_sortie>    Arrêt : Ctrl+C
// Requiert macOS 15+ (capture du micro via ScreenCaptureKit).
// Compilation : swiftc -O -parse-as-library -o meetrec meetrec.swift

import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

/// Une piste audio écrite dans un fichier, avec comblement des trous
/// pour que les deux pistes restent alignées dans le temps.
final class Track {
    let url: URL
    let name: String
    var file: AVAudioFile?
    var firstPTS: Double?
    var nextPTS: Double?
    var ferme = false

    init(url: URL, name: String) {
        self.url = url
        self.name = name
    }

    func append(_ sb: CMSampleBuffer) {
        // Fermée : un tampon tardif ne doit surtout pas recréer (donc écraser) le fichier.
        guard !ferme, let fd = sb.formatDescription else { return }
        // Format lu dans l'ASBD, pas via AVAudioFormat(cmAudioFormatDescription:) : sous Teams, le micro
        // arrive avec 3 canaux sans disposition et ce format-là fait planter AVAudioPCMBuffer (13/10).
        // Flottant 32 bits (micro interne) ou entier signé 16 bits (micro Bluetooth mains-libres, ex. earbuds).
        guard let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fd)?.pointee,
              asbd.mChannelsPerFrame > 0, asbd.mSampleRate > 0 else { return }
        let estFlottant = asbd.mFormatFlags & kAudioFormatFlagIsFloat != 0
        guard (estFlottant && asbd.mBitsPerChannel == 32)
                || (!estFlottant && asbd.mFormatFlags & kAudioFormatFlagIsSignedInteger != 0 && asbd.mBitsPerChannel == 16),
              let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: asbd.mSampleRate,
                                         channels: 1, interleaved: false) else { return }
        let octets = estFlottant ? 4 : 2
        let ch = Int(asbd.mChannelsPerFrame)
        let pts = sb.presentationTimeStamp.seconds
        let frames = sb.numSamples
        guard frames > 0, pts.isFinite else { return }

        do {
            if file == nil {
                file = try AVAudioFile(
                    forWriting: url,
                    settings: format.settings,
                    commonFormat: format.commonFormat,
                    interleaved: format.isInterleaved)
                firstPTS = pts
                nextPTS = pts
            }
            guard let file = file else { return }

            // Silence quand la source s'est tue sans envoyer de tampon, écrit par minutes pour garder
            // les deux pistes alignées même après une longue coupure (plafond 4 h contre une horloge folle).
            if let expected = nextPTS, pts - expected > 0.05, pts - expected < 4 * 3600 {
                var reste = AVAudioFrameCount((pts - expected) * format.sampleRate)
                let minute = AVAudioFrameCount(60 * format.sampleRate)
                if let silence = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: min(reste, minute)) {
                    memset(silence.floatChannelData![0], 0, Int(silence.frameCapacity) * 4)
                    while reste > 0 {
                        silence.frameLength = min(reste, silence.frameCapacity)
                        try file.write(from: silence)
                        reste -= silence.frameLength
                    }
                }
            }

            // Toujours écrit en mono (moyenne des canaux) : le reste de l'outil lit du float32 mono.
            func sample(_ p: UnsafeRawPointer, _ i: Int) -> Float {
                estFlottant ? p.load(fromByteOffset: i * 4, as: Float.self)
                            : Float(p.load(fromByteOffset: i * 2, as: Int16.self)) / 32768
            }
            try sb.withAudioBufferList { abl, _ in
                guard let buf = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)),
                      let out = buf.floatChannelData?[0] else { return }
                buf.frameLength = AVAudioFrameCount(frames)
                let sources = abl.compactMap { $0.mData.map { UnsafeRawPointer($0) } }
                guard sources.count == abl.count else { return }
                if sources.count == 1 {  // entrelacé : ch canaux côte à côte
                    guard Int(abl[0].mDataByteSize) >= frames * ch * octets else { return }
                    for i in 0..<frames {
                        var s: Float = 0
                        for c in 0..<ch { s += sample(sources[0], i * ch + c) }
                        out[i] = s / Float(ch)
                    }
                } else {  // un tampon par canal
                    guard abl.allSatisfy({ Int($0.mDataByteSize) >= frames * octets }) else { return }
                    for i in 0..<frames { out[i] = sources.reduce(0) { $0 + sample($1, i) } / Float(sources.count) }
                }
                try file.write(from: buf)
            }
            nextPTS = pts + Double(frames) / format.sampleRate
        } catch {
            fputs("[\(name)] erreur d'écriture : \(error)\n", stderr)
        }
    }
}

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let system: Track
    let mic: Track
    var stream: SCStream?
    let queue = DispatchQueue(label: "meetrec.audio")
    var arrete = false  // arrêt demandé : plus aucune relance

    init(dir: URL) {
        system = Track(url: dir.appendingPathComponent("systeme.wav"), name: "systeme")
        mic = Track(url: dir.appendingPathComponent("micro.wav"), name: "micro")
    }

    /// Relance la capture après une coupure : le 03/11, macOS a coupé le flux à 10:16 (« L'utilisateur a
    /// interrompu la diffusion ») et 1 h 30 d'atelier ont été perdues. Les pistes restent alignées : Track
    /// comble le trou par du silence. Essais à 1, 2, 4, 8 puis toutes les 10 s, tant que l'arrêt n'est pas demandé.
    // ponytail: arrete et stream lus sans verrou depuis deux files ; tient pour un seul flux à la fois
    func relancer() {
        Task {
            var attente: UInt64 = 1
            while !arrete {
                try? await Task.sleep(nanoseconds: attente * 1_000_000_000)
                if arrete { return }
                do {
                    try await start()
                    journal("Capture relancée.")
                    return
                } catch {
                    journal("Relance impossible : \(error.localizedDescription)")
                    attente = min(attente * 2, 10)
                }
            }
        }
    }

    /// Coupure provoquée (kill -USR1) : seul moyen de vérifier la relance sans attendre que macOS coupe.
    func couper() {
        let s = stream
        Task {
            try? await s?.stopCapture()
            journal("Coupure provoquée.")
            relancer()
        }
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else {
            throw NSError(domain: "meetrec", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Aucun écran trouvé"])
        }
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48_000
        config.channelCount = 1
        // Vidéo réduite au minimum : seule l'audio nous intéresse.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.captureMicrophone = true

        let s = SCStream(filter: filter, configuration: config, delegate: self)
        try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try s.addStreamOutput(self, type: .microphone, sampleHandlerQueue: queue)
        try await s.startCapture()
        stream = s
    }

    func stop() {
        arrete = true
        // Fichiers fermés d'abord, sur la file audio : plus rien ne s'écrit ensuite.
        queue.sync {
            for t in [system, mic] {
                t.ferme = true
                t.file = nil  // ferme et finalise le fichier
            }
        }
        // stopCapture peut ne jamais rendre la main : on ne l'attend pas, la sortie du processus coupe le flux.
        let s = stream
        Task { try? await s?.stopCapture() }
    }

    func writeTimings(to url: URL) {
        var d: [String: Double] = [:]
        if let s = system.firstPTS { d["systeme_debut"] = s }
        if let m = mic.firstPTS { d["micro_debut"] = m }
        if let data = try? JSONSerialization.data(withJSONObject: d, options: .prettyPrinted) {
            try? data.write(to: url)
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard sb.isValid else { return }
        switch type {
        case .audio:
            system.append(sb)
        case .microphone:
            mic.append(sb)
        default:
            break  // vidéo : ignorée, mais la sortie doit exister sinon ScreenCaptureKit perd des trames
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        journal("Capture arrêtée : \(error.localizedDescription)")
        // Un ancien flux qui signale son arrêt après une relance ne doit pas en lancer une seconde.
        if stream === self.stream, !arrete { relancer() }
    }
}

/// Une ligne horodatée dans le journal (stderr, rangé dans meetrec.log par le serveur).
func journal(_ message: String) {
    let f = DateFormatter()
    f.dateFormat = "HH:mm:ss"
    fputs("[\(f.string(from: Date()))] \(message)\n", stderr)
}

/// Arrêt propre sur Ctrl+C, kill ou fermeture du terminal. Installé dès le lancement :
/// un signal reçu pendant le démarrage de la capture est retenu, pas perdu ni fatal.
enum Arret {
    static let recu = DispatchSemaphore(value: 0)
    static var sources: [DispatchSourceSignal] = []

    static func installer() {
        signal(SIGUSR1, SIG_IGN)  // coupure provoquée : jamais fatale, même reçue pendant le démarrage
        for sig in [SIGINT, SIGTERM, SIGHUP] {
            signal(sig, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .global())
            src.setEventHandler { recu.signal() }
            src.resume()
            sources.append(src)
        }
    }

    static func attendre() async {
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            DispatchQueue.global().async {
                recu.wait()
                c.resume()
            }
        }
    }
}

@main
struct MeetRec {
    static func main() async {
        let args = CommandLine.arguments
        guard args.count >= 2 else {
            fputs("Usage : meetrec <dossier_sortie>\n", stderr)
            exit(2)
        }
        let dir = URL(fileURLWithPath: args[1], isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        Arret.installer()
        let rec = Recorder(dir: dir)
        do {
            try await rec.start()
        } catch {
            fputs("""
                Impossible de démarrer : \(error.localizedDescription)
                Dans Réglages Système > Confidentialité et sécurité, autorise l'app qui lance l'enregistrement
                (Réunion, ou ton terminal) dans "Enregistrement de l'écran et de l'audio système" et "Micro",
                puis relance-la.

                """, stderr)
            exit(1)
        }

        let usr1 = DispatchSource.makeSignalSource(signal: SIGUSR1, queue: .global())
        usr1.setEventHandler { rec.couper() }
        usr1.resume()

        print("Enregistrement en cours dans \(dir.path). Ctrl+C pour arrêter.")
        await Arret.attendre()
        print("\nArrêt...")
        rec.stop()
        rec.writeTimings(to: dir.appendingPathComponent("pistes.json"))
        print("Fichiers écrits.")
        exit(0)
    }
}
